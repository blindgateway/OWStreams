"""FastAPI dependencies.

Authentication, authorisation and per-request rate limiting live here so that
every route gets the same guarantees and a reviewer can read them in one place.

Three rules the routes rely on:

* **Authorisation is never implied by a path parameter.**  Every media,
  history and favourite access is scoped through :func:`require_profile`, which
  checks that the profile belongs to the *authenticated* user.  Passing another
  account's ``profile_id`` yields 404, not 403 -- see SECURITY.md.
* **Rate limiting happens before the handler runs**, keyed on the identity that
  is actually being charged (user when known, address otherwise).
* **A ``must_change_password`` account can still read**, because the client
  needs to render the change-password screen; it simply cannot mutate anything
  except its own credentials.
"""

from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from ..config import Config
from ..db.database import Database
from ..security import tokens
from ..security.audit import SecurityEventTypes
from ..security.paths import MediaRootRegistry
from ..security.rate_limit import RateLimiter
from ..services.auth import AuthService
from ..services.history import HistoryService, RatingService
from ..services.importer import Importer
from ..services.library import LibraryService
from ..services.profiles import ProfileError, ProfileService

log = logging.getLogger(__name__)

#: ``auto_error=False`` so a missing header produces our own 401 shape rather
#: than FastAPI's, which keeps the client error handling uniform.
bearer_scheme = HTTPBearer(auto_error=False, description="Access or playback token")


@dataclass(slots=True)
class Caller:
    """Everything the handlers need to know about who is asking."""

    user: dict
    claims: tokens.TokenClaims
    ip: str | None = None
    request_id: str = ""

    @property
    def user_id(self) -> int:
        return int(self.user["id"])

    @property
    def is_admin(self) -> bool:
        return bool(self.user.get("is_admin"))

    @property
    def must_change_password(self) -> bool:
        return bool(self.user.get("must_change_password"))


@dataclass(slots=True)
class AppContext:
    """Everything the app was built with.  Attached to ``app.state``."""

    config: Config
    db: Database
    registry: MediaRootRegistry
    auth: AuthService
    profiles: ProfileService
    library: LibraryService
    history: HistoryService
    ratings: RatingService
    importer: Importer
    limiter: RateLimiter
    audit: object
    started_at: float
    secret: bytes

    def close(self) -> None:
        self.db.close()


def get_context(request: Request) -> AppContext:
    context = getattr(request.app.state, "context", None)
    if context is None:  # pragma: no cover - only if the app was mis-built
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="application context is not initialised",
        )
    return context


Ctx = Annotated[AppContext, Depends(get_context)]


def client_ip(request: Request, context: AppContext) -> str | None:
    """The caller's address, honouring X-Forwarded-For only from a trusted proxy.

    Trusting the header unconditionally would let any client spoof its way past
    the login rate limiter, which is exactly the abuse it exists to stop.
    """
    peer = request.client.host if request.client else None
    trusted = set(context.config.server.trusted_proxies)
    if peer and peer in trusted:
        forwarded = request.headers.get("x-forwarded-for", "")
        first = forwarded.split(",")[0].strip()
        if first and len(first) <= 45:
            return first
    return peer


def _enforce_limit(
    context: AppContext, bucket: str, key: str, *, request: Request
) -> None:
    allowed, remaining, retry_after = context.limiter.allow(bucket, key)
    request.state.rate_remaining = remaining
    if allowed:
        return
    context.audit.record(
        SecurityEventTypes.RATE_LIMITED, outcome="denied",
        ip_address=client_ip(request, context), detail={"bucket": bucket},
    )
    raise HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail=f"Rate limit exceeded for {bucket}. Try again in "
               f"{max(1, int(retry_after))}s.",
        headers={"Retry-After": str(max(1, int(retry_after)))},
    )


def require_caller(
    request: Request,
    context: Ctx,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)] = None,
) -> Caller:
    """Authenticate a request.  Also the single place that checks revocation."""
    if credentials is None or not credentials.credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token_value = credentials.credentials
    ip = client_ip(request, context)

    try:
        claims = tokens.parse_access_token(context.secret, token_value)
    except tokens.TokenError as exc:
        context.audit.record(
            SecurityEventTypes.TOKEN_REJECTED, outcome="denied",
            ip_address=ip, detail={"reason": exc.code},
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Session is invalid or has expired. Sign in again.",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc

    user = context.auth.verify(claims)
    if user is None:
        context.audit.record(
            SecurityEventTypes.TOKEN_REJECTED, outcome="denied",
            user_id=claims.sub, ip_address=ip,
            detail={"reason": "inactive_or_missing_user"},
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Account is no longer active.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Explicit logout kills the jti immediately; short token lifetimes make this
    # a belt-and-braces measure for the remaining seconds.
    if context.db.query_one(
        "SELECT 1 FROM revoked_tokens WHERE jti = ? AND expires_at > datetime('now')",
        (claims.jti,),
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Session has been signed out.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # A session revoked from another device also invalidates its access token.
    if claims.session_id:
        row = context.db.query_one(
            "SELECT revoked_at FROM sessions WHERE id = ?", (claims.session_id,)
        )
        if row is None or row["revoked_at"]:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Session has been ended.",
                headers={"WWW-Authenticate": "Bearer"},
            )

    _enforce_limit(context, "api", f"user:{claims.sub}", request=request)
    return Caller(user=user, claims=claims, ip=ip,
                  request_id=getattr(request.state, "request_id", ""))


CallerDep = Annotated[Caller, Depends(require_caller)]


def require_admin(caller: CallerDep) -> Caller:
    if not caller.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This action requires an administrator account.",
        )
    return caller


AdminDep = Annotated[Caller, Depends(require_admin)]


def require_profile(
    request: Request,
    context: Ctx,
    caller: CallerDep,
    x_profile_id: Annotated[str | None, Header(alias="X-Profile-Id")] = None,
) -> int:
    """Resolve and authorise the acting profile for this request.

    The profile may be named by the ``X-Profile-Id`` header or, when absent,
    taken from the token.  Either way it must belong to the caller.
    """
    profile_id: int | None = None
    if x_profile_id:
        try:
            profile_id = int(x_profile_id)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="X-Profile-Id must be an integer.",
            ) from exc
    if profile_id is None:
        profile_id = caller.claims.profile_id

    try:
        resolved = context.profiles.require(caller.user_id, profile_id)
    except ProfileError as exc:
        status_code = exc.status_code
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc

    request.state.profile_id = resolved
    return resolved


ProfileDep = Annotated[int, Depends(require_profile)]


def require_media(profile_id: ProfileDep, media_id: int, context: Ctx) -> dict:
    """Load a media row and refuse to serve it when the file is gone."""
    from ..db import repositories as repo

    media = repo.get_media_full(context.db, int(media_id))
    if not media or media.get("hidden"):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Media not found."
        )
    return media


def require_uncapped(caller: CallerDep) -> Caller:
    """Reject mutations from an account still on the shipped default password.

    Reads stay available so the client can render the change-password screen
    without a second code path.
    """
    if caller.must_change_password:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Change your password before continuing.",
            headers={"X-Requires-Password-Change": "true"},
        )
    return caller


UncappedDep = Annotated[Caller, Depends(require_uncapped)]


def new_request_id() -> str:
    return secrets.token_hex(8)