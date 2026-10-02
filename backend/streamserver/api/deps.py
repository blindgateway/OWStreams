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


def require_caller_for(
    request: Request,
    context: AppContext,
    credentials: HTTPAuthorizationCredentials | None,
    *,
    allow_media_token: bool = False,
) -> Caller:
    """Authenticate a request.  Also the single place that checks revocation.

    A *playback* token is refused here: it authorises exactly one thing --
    streaming the title it names -- and every other route must say so rather
    than quietly treating it as a session token.
    """
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

    # A playback token authorises exactly one thing: streaming one title to one
    # profile.  It is a *different kind* of credential, so anything that needs a
    # session -- reading the library, reaching the admin API -- must refuse it
    # rather than silently treating it as a session token.
    if claims.typ == tokens.TOKEN_TYPE_MEDIA and not allow_media_token:
        context.audit.record(
            SecurityEventTypes.TOKEN_REJECTED, outcome="denied",
            user_id=claims.sub, ip_address=ip,
            detail={"reason": "media_token_used_for_session_api"},
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This is a playback token. It only authorises streaming the "
                   "title it was issued for.",
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


def require_caller(
    request: Request,
    context: Ctx,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)] = None,
) -> Caller:
    """Authenticate any request that needs a *session* token."""
    return require_caller_for(request, context, credentials)


def require_stream_caller(
    request: Request,
    context: Ctx,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)] = None,
) -> Caller:
    """Authenticate a streaming request, accepting a playback token.

    The streaming endpoint, and only it, accepts a playback token.  Every other
    check is the same one every other route goes through; the token-scope rule
    is skipped so the streaming handler can apply its own, stricter binding
    (this title, this profile).

    Declared synchronously on purpose: an async dependency is not resolved in
    a worker thread, so calling :func:`require_caller_for` from inside one would
    make FastAPI try to satisfy its parameters from the query string.
    """
    return require_caller_for(request, context, credentials,
                              allow_media_token=True)


def require_admin(caller: UncappedCallerDep) -> Caller:
    """Administrator-only, on top of the default-password gate.

    Two gates, one dependency, so that neither can be left off by accident:
    :func:`require_admin` alone let any flagged administrator rewrite the users
    table, and the default-password gate alone left every admin route
    ungated.  Since the gate exempts administrators and this one does not, the
    combination is exactly "not an administrator, and not on a default
    password".
    """
    if not caller.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This action requires an administrator account.",
        )
    return caller


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
    return _resolve_profile(request, context, caller, x_profile_id)


def _resolve_profile(
    request: Request,
    context: AppContext,
    caller: Caller,
    x_profile_id: str | None,
) -> int:
    """Validate the acting profile and check it belongs to the caller.

    A profile named by neither the header nor the token falls back to the
    account's first, and whether that is acceptable is the caller's decision --
    see :func:`require_own_profile`.
    """
    from ..db import repositories as repo

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

    if profile_id is None:
        # No header and no profile in the token.  A session token names an
        # *account*; every profile-scoped route needs a *profile*, and the
        # first request after sign-in has not named one yet.
        #
        # For an account-scoped read -- the catalogue itself, a title's
        # metadata, a poster -- the account's first profile is a sound answer,
        # and refusing would make every such read an error for no benefit.  For
        # anything that is per profile *by definition* -- favourites, history,
        # ratings, the home rails -- it is not: the caller would be answered
        # with somebody's list, and no header of their own would change it.
        # `require_own_profile` is that distinction, made per route.
        profiles = repo.list_profiles(context.db, caller.user_id)
        if not profiles:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error": "no_profile",
                        "message": "This account has no profiles."},
            )
        profile_id = int(profiles[0]["id"])

    try:
        resolved = context.profiles.require(caller.user_id, profile_id)
    except ProfileError as exc:
        # The dict form, so the body keeps `exc.code`.  The bare-string form
        # this used to pass made `http_exception_handler` fall back to
        # `_code_for_status`, so a foreign profile answered
        # `{"error": "not_found"}` -- indistinguishable from a missing title,
        # and from the documented `profile_not_found` every other profile
        # failure carries.
        raise HTTPException(status_code=exc.status_code,
                             detail={"error": exc.code,
                                     "message": str(exc)}) from exc

    request.state.profile_id = resolved
    return resolved


def require_own_profile(request: Request, context: Ctx, caller: CallerDep,
                        x_profile_id: Annotated[str | None,
                                               Header(alias="X-Profile-Id")] = None,
                        ) -> int:
    """:func:`require_profile` for routes whose answer belongs to one profile.

    Favourites, watch history, ratings and the home rails are per profile, so a
    request that names none must not be answered with the account's first
    profile: two people sharing an account would see each other's lists, and the
    client would have no way to tell -- its own `X-Profile-Id` would simply be
    ignored.

    A playback token *is* scoped: it names both a title and a profile, so a
    streaming request needs no header at all.
    """
    if not x_profile_id and caller.claims.profile_id is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "profile_required",
                "message": "No profile was named. Send X-Profile-Id, or use a "
                           "playback token, which names its profile.",
            },
        )
    return _resolve_profile(request, context, caller, x_profile_id)


def require_media(profile_id: ProfileDep, media_id: int, context: Ctx) -> dict:
    """Load a media row and refuse to serve it when the file is gone."""
    from ..db import repositories as repo

    media = repo.get_media_full(context.db, int(media_id))
    if not media or media.get("hidden"):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Media not found."
        )
    return media


def require_stream_profile(
    request: Request,
    context: Ctx,
    caller: StreamCallerDep,
    x_profile_id: Annotated[str | None, Header(alias="X-Profile-Id")] = None,
) -> int:
    """:func:`require_profile` for the streaming route.

    It resolves the caller through ``StreamCallerDep`` rather than
    ``CallerDep``.  With a shared alias, FastAPI's dependency cache hands the
    *first* resolved caller to both -- so the stream route ended up with a
    ``Caller`` authenticated as a session token while believing it had accepted
    a playback token.  Two aliases keep the two credential kinds apart.
    """
    return _resolve_profile(request, context, caller, x_profile_id)


def _is_stream(request: Request) -> bool:
    """Whether this is the streaming route, whatever id it carries.

    Matched on a path *segment*: the route is ``/api/v1/stream/7``, so neither
    a prefix nor a suffix is the whole truth.
    """
    return "stream" in request.url.path.split("/")


def require_uncapped(request: Request, caller: CallerDep) -> Caller:
    """Reject mutations from an account still on the shipped default password.

    Reads stay available so the client can render the change-password screen
    without a second code path.

    Enforced for every method that mutates, rather than by each route opting
    in.  Wiring it per route left the gate open: the mutating playback and
    progress routes never took the dependency, so an account on the shipped
    default password could rate, favourite, start and close a playback session
    and write watch history -- the state the first-run flow exists to stop
    somebody else leaving behind.  ``test_the_gate_is_wired_to_every_mutating
    _route`` in ``tests/test_client_routes.py`` now walks the routers and
    refuses to let a mutating route come back ungated.

    Administrators are exempt.  The documented first-run is ``add-user
    --admin``, and the CLI has no change-password flow, so gating them would
    leave the administrator permanently unable to stream anything.

    Refused with 403 plus ``X-Requires-Password-Change: true``, which is what
    the client watches for.
    """
    # The one GET that also writes: streaming records "started watching" in the
    # history before the first byte is served, so a method test on its own
    # would hand a capped account the exact state this gate protects.
    if request.method in ("GET", "HEAD", "OPTIONS") and not _is_stream(request):
        return caller
    if caller.must_change_password and not caller.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Change your password before continuing.",
            headers={"X-Requires-Password-Change": "true"},
        )
    return caller


def require_uncapped_caller(request: Request, caller: CallerDep) -> Caller:
    """:func:`require_uncapped` for a route that authenticates with a session."""
    return require_uncapped(request, caller)


def require_uncapped_stream_caller(request: Request,
                                   caller: StreamCallerDep) -> Caller:
    """:func:`require_uncapped` for the streaming route.

    Separate from the above for the same reason as :data:`StreamCallerDep`:
    sharing one alias would hand the streaming route's caller -- possibly
    authenticated by a playback token -- to a session route.
    """
    return require_uncapped(request, caller)


def new_request_id() -> str:
    return secrets.token_hex(8)


# ---------------------------------------------------------------------------
# Dependency aliases
#
# Defined at the end, once every function above exists, and imported *by
# value* at the call sites.  Under ``from __future__ import annotations`` a
# string annotation would never be resolved by FastAPI: the dependency would
# silently not be wired, and FastAPI would instead look for a query parameter
# with the parameter's name.
# ---------------------------------------------------------------------------
CallerDep = Annotated[Caller, Depends(require_caller)]
StreamCallerDep = Annotated[Caller, Depends(require_stream_caller)]
AdminDep = Annotated[Caller, Depends(require_admin)]
ProfileDep = Annotated[int, Depends(require_profile)]
OwnProfileDep = Annotated[int, Depends(require_own_profile)]
StreamProfileDep = Annotated[int, Depends(require_stream_profile)]

# One per mutating route, rather than one shared alias, for the same reason the
# stream route has its own caller: FastAPI's dependency cache hands the first
# resolved value to every route that names the same alias, so a shared
# `Caller`-returning dependency would leak one route's authentication into
# another's.  Each below resolves through `require_caller` and then applies the
# default-password gate.
UncappedCallerDep = Annotated[Caller, Depends(require_uncapped_caller)]
UncappedStreamCallerDep = Annotated[Caller, Depends(require_uncapped_stream_caller)]
