"""FastAPI application factory.

``create_app`` builds a fully wired application from a :class:`Config`.  Nothing
is read from global state, so tests can build as many independent instances as
they like -- each with its own database, signing key and library roots.

Middleware
----------
``RequestContextMiddleware``  assigns a request id, records timing, and logs
one line per *session* rather than per Range request.
``SecurityHeadersMiddleware``  sets the response headers that cost nothing and
close off whole classes of mistake.
``TrustedHostMiddleware``      rejects unexpected ``Host`` headers.
"""

from __future__ import annotations

import logging
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .. import API_VERSION, __version__
from ..config import Config, load_config
from ..db.database import Database, migrate
from ..db.migrations import MIGRATIONS
from ..media.probe import MediaProbe
from ..metadata.providers import ProviderChain, build_provider
from ..security.audit import AuditLogger
from ..security.paths import MediaRootRegistry
from ..security.rate_limit import RateLimiter
from ..security.tokens import load_or_create_secret
from ..services.auth import AuthService
from ..services.history import HistoryService, RatingService
from ..services.importer import Importer
from ..services.library import LibraryService
from ..services.profiles import ProfileService
from .deps import AppContext
from .errors import install_handlers
from .v1 import api_router

log = logging.getLogger("streamserver.api")

#: Server-side only.  The client fetches images through authenticated routes.
INTERNAL_NETWORKS = {"127.0.0.1", "::1", "10.0.0.0/8", "172.16.0.0/12",
                     "192.168.0.0/16", "100.64.0.0/10", "fc00::/7"}


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Request id, timing, and one log line per client session.

    Streaming produces one request per 8 MiB chunk -- thousands for a feature
    film.  Logging each would fill the disk on a 10 GB SSD and bury every other
    event, so consecutive streaming requests for the same client are folded
    into a single summary line.
    """

    def __init__(self, app, audit: AuditLogger) -> None:
        super().__init__(app)
        self.audit = audit
        self._last_stream_log: dict[str, tuple[float, int, int]] = {}

    async def dispatch(self, request: Request, call_next):
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex[:16]
        request.state.request_id = request_id
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            raise
        duration_ms = (time.perf_counter() - started) * 1000

        is_stream = request.url.path.startswith("/api/v1/stream/")
        client = request.client.host if request.client else "unknown"
        response.headers["X-Request-Id"] = request_id
        response.headers["X-Response-Time-Ms"] = f"{duration_ms:.1f}"

        if not is_stream:
            log.info("%s %s -> %s in %.1fms", request.method, request.url.path,
                     response.status_code, duration_ms)
            if response.status_code >= 500:
                self.audit.record("http.error", outcome="error", ip_address=client,
                                  request_id=request_id,
                                  detail={"path": request.url.path,
                                          "status": response.status_code})
        else:
            served = int(response.headers.get("content-length", "0") or 0)
            key = f"{client}:{request.url.path}"
            previous = self._last_stream_log.get(key)
            now = time.monotonic()
            if previous is None or now - previous[0] > 300:
                self._last_stream_log[key] = (now, served, 1)
            else:
                total_bytes, count = previous[1] + served, previous[2] + 1
                self._last_stream_log[key] = (previous[0], total_bytes, count)
                log.info("stream %s client=%s cumulative=%s bytes over %s chunks",
                         request.url.path, client, total_bytes, count)
                if len(self._last_stream_log) > 64:
                    self._last_stream_log.pop(next(iter(self._last_stream_log)), None)

        return response


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Response headers that close off common mistakes at zero cost."""

    def __init__(self, app, *, csp: bool) -> None:
        super().__init__(app)
        self.csp = csp

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("Cross-Origin-Resource-Policy", "same-origin")
        response.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
        response.headers.setdefault(
            "Permissions-Policy",
            "accelerometer=(), camera=(), geolocation=(), gyroscope=(), "
            "magnetometer=(), microphone=(), payment=(), usb=()",
        )
        # A streaming API has no reason to be cached by an intermediary.
        response.headers.setdefault("Cache-Control", "no-store")
        if self.csp:
            response.headers.setdefault(
                "Content-Security-Policy",
                "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; "
                "form-action 'none'",
            )
        return response


def _build_context(config: Config) -> AppContext:
    config.ensure_directories()

    db = Database(config.database_file)
    migrate(db, MIGRATIONS)

    secret = load_or_create_secret(config.database_file)
    audit = AuditLogger(db, enabled=True)
    limiter = RateLimiter()

    limits = config.rate_limit
    limiter.configure("login", limits.login_per_minute)
    limiter.configure("login_lockout", limits.login_lockout_per_minute)
    limiter.configure("api", limits.api_per_minute)
    limiter.configure("stream", limits.stream_per_minute)
    limiter.configure("search", limits.search_per_minute)
    limiter.configure("import", limits.import_per_minute)

    registry = MediaRootRegistry(tuple(Path(p) for p in config.media.library_roots))

    provider_name = config.metadata.default_provider
    provider = build_provider(
        provider_name,
        api_key=config.metadata.tmdb_api_key or config.metadata.imdb_api_key,
        timeout=config.metadata.request_timeout_seconds,
        max_image_bytes=config.metadata.max_image_bytes,
    )
    # The local sidecar provider always runs first: a hand-written .nfo should
    # beat a fuzzy web match, and it works with no network at all.
    chain = ProviderChain([build_provider("local"), provider])

    context = AppContext(
        config=config,
        db=db,
        registry=registry,
        auth=AuthService(db, config.auth, secret, limiter=limiter, audit=audit),
        profiles=ProfileService(db, audit=audit),
        library=LibraryService(db, config.media),
        history=HistoryService(db, config.media),
        ratings=RatingService(db),
        importer=Importer(db, config, registry, provider=chain, audit=audit),
        limiter=limiter,
        audit=audit,
        started_at=time.time(),
        secret=secret,
    )

    if registry.is_empty and config.media.library_roots:
        log.warning(
            "no usable library roots: %s", config.media.library_roots
        )
    if not MediaProbe(config.media.ffprobe_path).available:
        log.warning(
            "ffprobe is not available; technical metadata (codec, resolution, "
            "duration) will not be populated"
        )
    return context


def create_app(
    config: Config | None = None,
    *,
    config_path: str | Path | None = None,
) -> FastAPI:
    """Build the application.

    Passing an explicit ``config`` is what the tests do; passing ``config_path``
    or nothing is what the CLI and uvicorn entry point do.
    """
    resolved = config or load_config(config_path)
    context = _build_context(resolved)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        log.info(
            "streamserver %s (API %s) starting: %d library roots, data at %s",
            __version__, API_VERSION, len(context.registry.list_roots()),
            resolved.storage.data_dir,
        )
        try:
            yield
        finally:
            log.info("streamserver shutting down")
            context.close()

    app = FastAPI(
        title="Stream server",
        version=__version__,
        description=(
            "Personal media library, playback and per-profile state. "
            "See API.md for the full contract and SECURITY.md for the threat model."
        ),
        lifespan=lifespan,
        docs_url="/api/docs",
        redoc_url=None,               # one interactive surface is enough
        openapi_url="/api/openapi.json",
    )
    app.state.context = context

    install_handlers(app)
    app.add_middleware(SecurityHeadersMiddleware, csp=True)
    app.add_middleware(RequestContextMiddleware, audit=context.audit)
    if resolved.server.cors_origins:
        # Off by default.  When a browser client genuinely needs it, the allow
        # list is explicit -- never "*" with credentials.
        app.add_middleware(
            CORSMiddleware,
            allow_origins=resolved.server.cors_origins,
            allow_credentials=False,
            allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
            allow_headers=["Authorization", "Content-Type", "X-Profile-Id",
                           "X-Pin-Attempt", "X-Request-Id"],
            max_age=600,
        )
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=_allowed_hosts(resolved),
    )

    @app.get("/api/health", tags=["health"], summary="Liveness and readiness")
    async def health() -> JSONResponse:
        """Unauthenticated, and deliberately boring.

        It reports whether the process is up and whether the database answers --
        never a version-dependent capability list, which would help someone
        fingerprint which exploits apply.
        """
        try:
            db_ok, _ = context.db.integrity_check()
            healthy = True
        except Exception:  # noqa: BLE001
            db_ok, healthy = False, False
        return JSONResponse(
            status_code=200 if healthy else 503,
            content={
                "status": "ok" if healthy else "degraded",
                "database": "ok" if db_ok else "unavailable",
                "version": __version__,
                "api_version": API_VERSION,
            },
        )

    @app.get("/", include_in_schema=False)
    async def root() -> JSONResponse:
        return JSONResponse({
            "name": "Stream server",
            "version": __version__,
            "api_version": API_VERSION,
            "docs": "/api/docs",
            "health": "/api/health",
        })

    app.include_router(api_router, prefix=f"/api/{API_VERSION}")
    return app


def _allowed_hosts(config: Config) -> list[str]:
    """Hostnames this deployment answers to.

    Derived from the bind address, the machine's own hostname and any
    ``[server] allowed_hosts`` entries, so a LAN or Tailscale deployment works
    out of the box while a DNS-rebinding attempt with an unrelated ``Host``
    header does not.

    A server bound to a routable interface (``0.0.0.0``) is reachable by an
    address we cannot know ahead of time, so an operator who has not listed its
    names gets the wildcard.  That is the documented trade-off, and it is why
    the allow-list matters most on a loopback-bound server.
    """
    import socket

    hosts = {"localhost", "127.0.0.1", "[::1]", "::1", config.server.host}
    try:
        hosts.add(socket.gethostname())
        hosts.add(socket.getfqdn())
    except OSError:
        pass
    hosts.discard("")
    hosts.discard("0.0.0.0")
    hosts.discard("::")

    if config.server.allowed_hosts:
        return sorted(set(config.server.allowed_hosts))

    if config.server.host in {"0.0.0.0", "::", ""}:
        log.warning(
            "server.host is %r, so every Host header is accepted; set "
            "[server] allowed_hosts if this host is reachable by name",
            config.server.host or "*",
        )
        return ["*"]
    return sorted(hosts)


def run(config: Config | None = None, *, config_path: str | Path | None = None) -> None:
    """Start uvicorn.  Kept here so the CLI is a thin wrapper."""
    import uvicorn

    resolved = config or load_config(config_path)
    uvicorn.run(
        create_app(resolved),
        host=resolved.server.host,
        port=resolved.server.port,
        log_level=resolved.storage.log_level.lower(),
        # Multiple workers are not appropriate: SQLite in WAL mode wants a
        # single writer process, and the rate limiter is per-process state.
        workers=1,
        timeout_keep_alive=30,
        h11_max_incomplete_event_size=64 * 1024,
    )


__all__ = ["create_app", "run", "AppContext"]