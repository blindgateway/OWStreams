"""Authentication and account routes.

Endpoints
---------
``POST /auth/login``           sign in, get an access + refresh token pair
``POST /auth/refresh``         rotate the pair
``POST /auth/logout``          end this session
``POST /auth/logout-all``      end every session for this account
``GET  /auth/me``              current account and profiles
``POST /auth/password``        change own password
``GET  /auth/sessions``        list active devices
``DELETE /auth/sessions/{id}`` revoke one device
``GET  /auth/setup``           is first-run setup still needed?
``POST /auth/setup``           create the first account
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, status

from ...db import repositories as repo
from ...services.auth import validate_password_strength
from ..deps import AdminDep, CallerDep, Ctx, client_ip
from ..schemas import (
    LoginRequest,
    OkResponse,
    PasswordChangeRequest,
    PasswordResetRequest,
    ProfileOut,
    RefreshRequest,
    ServerInfo,
    SessionOut,
    SetupRequest,
    TokenResponse,
    UserSummary,
)

router = APIRouter(prefix="/auth", tags=["authentication"])


def _user_summary(user: dict) -> UserSummary:
    return UserSummary(
        id=int(user["id"]),
        username=user["username"],
        display_name=user.get("display_name") or user["username"],
        is_admin=bool(user.get("is_admin")),
        must_change_password=bool(user.get("must_change_password")),
        email=user.get("email"),
        last_login_at=user.get("last_login_at"),
    )


@router.post("/login", response_model=TokenResponse,
             summary="Sign in with a username and password")
async def login(request: Request, context: Ctx,
                payload: LoginRequest) -> TokenResponse:
    """Authenticate and issue a token pair.

    Failure responses are deliberately uniform: a wrong password, an unknown
    username and a disabled account all return the same 401 with the same
    message, and the work done is the same, so neither the response nor its
    timing distinguishes them.
    """
    pair = context.auth.login(
        username=payload.username,
        password=payload.password,
        device_name=payload.device_name or _device_name(request),
        client_version=payload.client_version,
        ip_address=client_ip(request, context),
        user_agent=request.headers.get("user-agent"),
        remember=payload.remember,
    )
    # Opportunistically upgrade a stored hash when the KDF policy has been
    # raised since the password was set.
    context.auth.maybe_upgrade_hash(pair.user, payload.password)
    return TokenResponse(**pair.public())


def _device_name(request: Request) -> str:
    agent = request.headers.get("user-agent", "")
    for token, label in (
        ("Android", "Android"), ("iPhone", "iOS"), ("Windows", "Windows"),
        ("Qt", "Desktop client"), ("Mozilla", "Browser"),
    ):
        if token in agent:
            return label
    return "Unknown device"


@router.post("/refresh", response_model=TokenResponse,
             summary="Exchange a refresh token for a new token pair")
async def refresh(request: Request, context: Ctx, payload: RefreshRequest) -> TokenResponse:
    pair = context.auth.refresh(payload.refresh_token, ip_address=client_ip(request, context))
    return TokenResponse(**pair.public())


@router.post("/logout", response_model=OkResponse, summary="End this session")
async def logout(request: Request, context: Ctx, caller: CallerDep) -> OkResponse:
    context.auth.logout(caller.claims, ip_address=caller.ip)
    return OkResponse(detail="Signed out.")


@router.post("/logout-all", response_model=OkResponse,
             summary="End every session for this account")
async def logout_all(caller: CallerDep, context: Ctx) -> OkResponse:
    count = context.auth.logout_everywhere(caller.user_id)
    return OkResponse(detail=f"Signed out of {count} session(s).")


@router.get("/me", summary="Current account, profiles and capabilities")
async def me(request: Request, context: Ctx, caller: CallerDep) -> dict:
    profiles = context.profiles.list_for_user(caller.user_id)
    return {
        "user": _user_summary(caller.user).model_dump(),
        "profiles": [ProfileOut(**p.as_dict()).model_dump() for p in profiles],
        "server": _server_info(request, context, authenticated=True),
    }


@router.post("/password", response_model=OkResponse, summary="Change own password")
async def change_password(request: Request, context: Ctx, caller: CallerDep,
                          payload: PasswordChangeRequest) -> OkResponse:
    context.auth.change_password(
        caller.user_id,
        current_password=payload.current_password,
        new_password=payload.new_password,
        current_session=caller.claims.session_id,
        ip_address=caller.ip,
    )
    if payload.logout_other_sessions:
        repo.revoke_all_sessions(
            context.db, caller.user_id,
            except_session=caller.claims.session_id, reason="password_changed",
        )
    return OkResponse(detail="Password updated.")


@router.get("/sessions", response_model=list[SessionOut],
            summary="List this account's active and revoked sessions")
async def list_sessions(caller: CallerDep, context: Ctx) -> list[SessionOut]:
    rows = repo.list_sessions(context.db, caller.user_id)
    return [SessionOut(**row) for row in rows]


@router.delete("/sessions/{session_id}", response_model=OkResponse,
               summary="Revoke one device session")
async def revoke_session(session_id: str, caller: CallerDep, context: Ctx) -> OkResponse:
    revoked = context.auth.revoke_session(caller.user_id, session_id, reason="user_revoked")
    if not revoked:
        raise HTTPException(status_code=404, detail="Session not found.")
    return OkResponse(detail="Session revoked.")


@router.get("/setup", response_model=ServerInfo,
            summary="Whether first-run setup is still required")
async def setup_status(request: Request, context: Ctx) -> ServerInfo:
    return _server_info(request, context, authenticated=False)


@router.post("/setup", response_model=TokenResponse,
             status_code=status.HTTP_201_CREATED,
             summary="Create the first administrator account")
async def run_setup(request: Request, context: Ctx, payload: SetupRequest) -> TokenResponse:
    """First-run bootstrap.

    Only succeeds while the users table is empty, and does so inside one
    transaction together with the default profile -- so there is no window in
    which two concurrent setup requests can both create an administrator.
    """
    from ...security import passwords

    with context.db.write() as conn:
        existing = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        if existing:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="This server has already been set up. Sign in instead.",
            )
        validate_password_strength(payload.password, context.config.auth)
        now = payload.username
        conn.execute(
            "INSERT INTO users (username, display_name, password_hash, is_admin, "
            "must_change_password, password_changed_at) VALUES (?, ?, ?, 1, 0, "
            "datetime('now'))",
            (
                payload.username,
                payload.display_name or now,
                passwords.hash_password(
                    payload.password,
                    n=context.config.auth.scrypt_n, r=context.config.auth.scrypt_r,
                    p=context.config.auth.scrypt_p, maxmem=context.config.auth.scrypt_maxmem,
                ),
            ),
        )
        user_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        conn.execute(
            "INSERT INTO profiles (user_id, name, sort_order) VALUES (?, ?, 0)",
            (user_id, payload.profile_name or "Main"),
        )

    user = repo.get_user(context.db, user_id)
    assert user is not None
    pair = context.auth.login(
        username=user["username"], password=payload.password,
        device_name="Setup", ip_address=client_ip(request, context),
        user_agent=request.headers.get("user-agent"),
    )
    context.audit.record(
        "admin.setup.completed", outcome="ok", user_id=user_id,
        username=user["username"], ip_address=client_ip(request, context),
    )
    return TokenResponse(**pair.public())


@router.get("/server", response_model=ServerInfo, summary="Server capabilities")
async def server_info(request: Request, context: Ctx) -> ServerInfo:
    return _server_info(request, context, authenticated=False)


@router.post("/password/reset", response_model=OkResponse,
             summary="Administrative password reset")
async def admin_reset_password(user_id: int, caller: AdminDep, context: Ctx,
                               payload: PasswordResetRequest) -> OkResponse:
    context.auth.reset_password(user_id, payload.new_password, actor_id=caller.user_id)
    return OkResponse(detail="Password reset. The account must change it at next sign-in.")


def _server_info(request: Request, context, *, authenticated: bool) -> ServerInfo:
    from ... import __version__, API_VERSION
    from ...media.probe import MediaProbe

    user_count = int(context.db.scalar("SELECT COUNT(*) FROM users") or 0)
    profile_count = int(context.db.scalar("SELECT COUNT(*) FROM profiles") or 0)
    library_count = int(context.db.scalar("SELECT COUNT(*) FROM media WHERE hidden = 0") or 0)
    return ServerInfo(
        version=__version__,
        api_version=API_VERSION,
        requires_setup=user_count == 0,
        authenticated=authenticated,
        library_count=library_count,
        profile_count=profile_count,
        # Roots are exposed so the admin UI can show what is mounted, but only
        # their existence and path -- never their contents.
        library_roots=[str(p) for p in context.registry.list_roots()],
        ffprobe_available=MediaProbe(context.config.media.ffprobe_path).available,
        metadata_provider=context.config.metadata.default_provider,
        capabilities={
            "profiles": True,
            "watch_history": True,
            "favorites": True,
            "ratings": True,
            "playback": True,
            "collections": True,
            "series": True,
            "admin_import": True,
        },
    )


__all__ = ["router"]