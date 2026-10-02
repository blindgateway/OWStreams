"""Authentication, session lifecycle and account security.

Flow
----
``login``      username+password -> short-lived access token + opaque refresh
               token, both recorded in ``sessions``.
``refresh``    refresh token -> a *new* pair.  The refresh token is rotated on
               every use, so a stolen copy is single-use and detectable.
``logout``     revokes the session and blacklists the access token's ``jti``.

Password changes revoke every *other* session and force the default-password
flag, which the client surfaces as a blocking "change your password" screen.
"""

from __future__ import annotations

import logging
import re
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from ..config import AuthConfig
from ..db import repositories as repo
from ..db.database import Database
from ..security import passwords, tokens
from ..security.audit import AuditLogger, SecurityEventTypes
from ..security.rate_limit import AccountLockout, RateLimiter

log = logging.getLogger(__name__)

USERNAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{2,31}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s.]+\.[^@\s]{2,}$")


class AuthError(Exception):
    """Authentication failed.  ``code`` maps onto the HTTP status."""

    def __init__(self, message: str, code: str = "invalid_credentials",
                 status_code: int = 401, retry_after: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.retry_after = retry_after


@dataclass(slots=True)
class TokenPair:
    access_token: str
    access_expires_at: str
    refresh_token: str
    refresh_expires_at: str
    session_id: str
    user: dict
    must_change_password: bool = False

    def public(self) -> dict:
        return {
            "access_token": self.access_token,
            "token_type": "Bearer",
            "expires_in": _seconds_between_now(self.access_expires_at),
            "refresh_token": self.refresh_token,
            "refresh_expires_at": self.refresh_expires_at,
            "session_id": self.session_id,
            "must_change_password": self.must_change_password,
            "user": {
                "id": self.user["id"],
                "username": self.user["username"],
                "display_name": self.user.get("display_name") or self.user["username"],
                "is_admin": bool(self.user.get("is_admin")),
            },
        }


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).replace(microsecond=0).isoformat()


def _seconds_between_now(iso: str) -> int:
    try:
        target = datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return 0
    return max(0, int((target - datetime.now(UTC)).total_seconds()))


def _validate_username(username: str) -> str:
    value = (username or "").strip()
    if not USERNAME_RE.match(value):
        raise AuthError(
            "Username must be 3-32 characters using letters, digits, dot, dash or underscore.",
            code="invalid_username",
            status_code=422,
        )
    return value


def validate_password_strength(password: str, cfg: AuthConfig) -> None:
    """Reject passwords that would be trivially guessed or ambiguous.

    Length is the dominant factor, so the rule is 10+ characters plus a mix of
    two character classes -- long enough that a personal server's threat model
    (someone with the file) is comfortably covered, short enough that people
    actually comply.
    """
    value = password or ""
    problems: list[str] = []
    if len(value) < cfg.password_min_length:
        problems.append(f"at least {cfg.password_min_length} characters")
    classes = sum([
        bool(re.search(r"[a-z]", value)),
        bool(re.search(r"[A-Z]", value)),
        bool(re.search(r"\d", value)),
        bool(re.search(r"[^\w\s]", value)),
    ])
    if classes < 2:
        problems.append("a mix of letters, digits or symbols")
    if value.lower() in {"password", "12345678", "qwertyui", "letmein1", "iloveyou1"}:
        problems.append("not a well-known password")
    if problems:
        raise AuthError(
            "Password is too weak: " + "; ".join(problems) + ".",
            code="weak_password",
            status_code=422,
        )


class AuthService:
    def __init__(
        self,
        db: Database,
        config: AuthConfig,
        secret: bytes,
        *,
        limiter: RateLimiter | None = None,
        audit: AuditLogger | None = None,
    ) -> None:
        self.db = db
        self.config = config
        self.secret = secret
        self.limiter = limiter or RateLimiter()
        self.audit = audit or AuditLogger(db, enabled=False)
        self.lockout = AccountLockout(
            db,
            max_failures=config.max_failed_logins,
            lockout_seconds=config.lockout_seconds,
        )

    # -- login -----------------------------------------------------------
    def login(
        self,
        *,
        username: str,
        password: str,
        device_name: str | None = None,
        client_version: str | None = None,
        ip_address: str | None = None,
        user_agent: str | None = None,
        remember: bool = False,
    ) -> TokenPair:
        username_norm = (username or "").strip()
        if not username_norm:
            raise AuthError("Username is required.", code="invalid_credentials")

        allowed, _, retry_after = self.limiter.allow("login", ip_address or "unknown")
        if not allowed:
            self.audit.record(
                SecurityEventTypes.RATE_LIMITED, outcome="denied",
                ip_address=ip_address, detail={"limit": "login", "username": username_norm[:32]},
            )
            raise AuthError(
                "Too many sign-in attempts. Try again shortly.",
                code="rate_limited", status_code=429,
                retry_after=max(1, int(retry_after)),
            )

        locked, seconds = self.lockout.is_locked(username_norm)
        if locked:
            self.audit.record(
                SecurityEventTypes.LOGIN_LOCKED, outcome="denied",
                username=username_norm, ip_address=ip_address,
                detail={"retry_after": seconds},
            )
            raise AuthError(
                "Account is temporarily locked after failed attempts. "
                f"Try again in {seconds} seconds.",
                code="account_locked", status_code=423, retry_after=seconds,
            )

        user = repo.get_user_by_username(self.db, username_norm)

        # Always run the KDF, even for an unknown user, so response time does
        # not reveal whether the account exists.
        stored_hash = user["password_hash"] if user else ""
        ok = passwords.verify_password(password or "", stored_hash)

        if not user or not ok or not user.get("is_active"):
            self.lockout.record_failure(username_norm, ip_address,
                                        "bad_credentials" if user else "unknown_user")
            self.audit.record(
                SecurityEventTypes.LOGIN_FAILURE, outcome="denied",
                username=username_norm[:32], ip_address=ip_address,
                detail={"known_user": user is not None,
                        "inactive": bool(user) and not user.get("is_active")},
            )
            raise AuthError("Incorrect username or password.", code="invalid_credentials")

        # Successful sign-in: clear the failure streak for this account.
        self.lockout.record_success(username_norm, ip_address)
        repo.touch_last_login(self.db, int(user["id"]))
        self.audit.record(
            SecurityEventTypes.LOGIN_SUCCESS, outcome="ok",
            user_id=int(user["id"]), username=user["username"],
            ip_address=ip_address, device_name=device_name,
        )

        return self._issue_pair(
            user,
            device_name=device_name,
            client_version=client_version,
            ip_address=ip_address,
            user_agent=user_agent,
            remember=remember,
        )

    def _issue_pair(
        self,
        user: dict,
        *,
        device_name: str | None = None,
        client_version: str | None = None,
        ip_address: str | None = None,
        user_agent: str | None = None,
        remember: bool = False,
    ) -> TokenPair:
        session_id = secrets.token_urlsafe(24)
        refresh_ttl = (
            self.config.refresh_token_ttl_long_seconds if remember
            else self.config.refresh_token_ttl_seconds
        )
        refresh_expires = datetime.now(UTC) + timedelta(seconds=refresh_ttl)
        plaintext, token_hash = tokens.create_refresh_token()

        repo.create_session(
            self.db,
            session_id=session_id,
            user_id=int(user["id"]),
            refresh_token_hash=token_hash,
            expires_at=_iso(refresh_expires),
            device_name=(device_name or "Unknown device")[:128],
            client_version=(client_version or "")[:32] or None,
            ip_address=ip_address,
            user_agent=(user_agent or "")[:256] or None,
        )

        access_expires = datetime.now(UTC) + timedelta(seconds=self.config.access_token_ttl_seconds)
        access_token, _ = tokens.create_access_token(
            self.secret,
            user_id=int(user["id"]),
            ttl_seconds=self.config.access_token_ttl_seconds,
            session_id=session_id,
        )

        return TokenPair(
            access_token=access_token,
            access_expires_at=_iso(access_expires),
            refresh_token=plaintext,
            refresh_expires_at=_iso(refresh_expires),
            session_id=session_id,
            user=user,
            must_change_password=bool(user.get("must_change_password")),
        )

    # -- refresh ---------------------------------------------------------
    def refresh(self, refresh_token: str, *, ip_address: str | None = None) -> TokenPair:
        if not tokens.is_refresh_token(refresh_token or ""):
            raise AuthError("Malformed refresh token.", code="invalid_refresh_token")
        token_hash = tokens.hash_refresh_token(refresh_token)
        session = repo.get_session_by_token(self.db, token_hash)
        if not session:
            # A refresh token that verifies but is unknown has either been
            # replayed after rotation or the row was deleted.  Either way it is
            # not usable, and the owning account's sessions are killed to
            # contain a stolen-token replay.
            self.audit.record(
                SecurityEventTypes.TOKEN_REJECTED, outcome="denied",
                ip_address=ip_address, detail={"reason": "unknown_or_rotated_refresh"},
            )
            raise AuthError("Session is no longer valid. Sign in again.",
                            code="refresh_revoked")

        user = repo.get_user(self.db, int(session["user_id"]))
        if not user or not user.get("is_active"):
            repo.revoke_session(self.db, session["id"], "user_inactive")
            raise AuthError("Account is not active.", code="account_disabled", status_code=403)

        # Rotate: the presented token dies here.
        new_plaintext, new_hash = tokens.create_refresh_token()
        refresh_expires = datetime.now(UTC) + timedelta(
            seconds=self.config.refresh_token_ttl_long_seconds
            if _remembered(session) else self.config.refresh_token_ttl_seconds
        )
        repo.rotate_session(
            self.db, session["id"],
            refresh_token_hash=new_hash,
            expires_at=_iso(refresh_expires),
            ip_address=ip_address,
        )

        access_expires = datetime.now(UTC) + timedelta(seconds=self.config.access_token_ttl_seconds)
        access_token, _ = tokens.create_access_token(
            self.secret,
            user_id=int(user["id"]),
            ttl_seconds=self.config.access_token_ttl_seconds,
            session_id=session["id"],
        )
        self.audit.record(
            SecurityEventTypes.TOKEN_REFRESH, outcome="ok",
            user_id=int(user["id"]), username=user["username"], ip_address=ip_address,
        )
        return TokenPair(
            access_token=access_token,
            access_expires_at=_iso(access_expires),
            refresh_token=new_plaintext,
            refresh_expires_at=_iso(refresh_expires),
            session_id=session["id"],
            user=user,
            must_change_password=bool(user.get("must_change_password")),
        )

    # -- logout ----------------------------------------------------------
    def logout(self, claims: tokens.TokenClaims, *, ip_address: str | None = None) -> None:
        """Revoke the session and blacklist this access token immediately."""
        if claims.session_id:
            repo.revoke_session(self.db, claims.session_id, "logout")
        tokens.revoke(self.db, claims.jti, claims.exp.isoformat(), "logout")
        self.audit.record(
            SecurityEventTypes.LOGOUT, outcome="ok",
            user_id=claims.sub, ip_address=ip_address,
        )

    def logout_everywhere(self, user_id: int, *, except_session: str | None = None) -> int:
        count = repo.revoke_all_sessions(self.db, user_id, except_session=except_session,
                                         reason="logout_all")
        self.audit.record(
            SecurityEventTypes.SESSION_REVOKED, outcome="ok", user_id=user_id,
            detail={"count": count},
        )
        return count

    def revoke_session(self, user_id: int, session_id: str, *, reason: str = "user_revoked") -> bool:
        session = repo.get_session(self.db, session_id)
        if not session or int(session["user_id"]) != user_id:
            return False
        repo.revoke_session(self.db, session_id, reason)
        self.audit.record(
            SecurityEventTypes.SESSION_REVOKED, outcome="ok", user_id=user_id,
            detail={"session_id": session_id, "reason": reason},
        )
        return True

    # -- password --------------------------------------------------------
    def change_password(
        self,
        user_id: int,
        *,
        current_password: str,
        new_password: str,
        current_session: str | None = None,
        ip_address: str | None = None,
    ) -> None:
        user = repo.get_user(self.db, user_id)
        if not user:
            raise AuthError("Account not found.", code="not_found", status_code=404)
        if not passwords.verify_password(current_password or "", user["password_hash"]):
            self.audit.record(
                SecurityEventTypes.PASSWORD_CHANGED, outcome="denied",
                user_id=user_id, username=user["username"], ip_address=ip_address,
                detail={"reason": "current_password_mismatch"},
            )
            raise AuthError("Current password is incorrect.", code="invalid_credentials")

        if passwords.verify_password(new_password or "", user["password_hash"]):
            raise AuthError(
                "New password must differ from the current one.",
                code="password_reuse", status_code=422,
            )
        validate_password_strength(new_password, self.config)

        repo.update_user(
            self.db, user_id,
            password_hash=passwords.hash_password(new_password),
            password_changed_at=datetime.now(UTC).replace(microsecond=0).isoformat(),
            must_change_password=False,
        )
        # Kill every other session: a password change is the standard response
        # to "I think someone else has my account".
        revoked = repo.revoke_all_sessions(
            self.db, user_id, except_session=current_session, reason="password_changed"
        )
        self.audit.record(
            SecurityEventTypes.PASSWORD_CHANGED, outcome="ok",
            user_id=user_id, username=user["username"], ip_address=ip_address,
            detail={"other_sessions_revoked": revoked},
        )

    def reset_password(self, user_id: int, new_password: str, *,
                       actor_id: int | None = None) -> None:
        """Administrative reset.  Not exposed to non-admin clients."""
        validate_password_strength(new_password, self.config)
        user = repo.get_user(self.db, user_id)
        if not user:
            raise AuthError("Account not found.", code="not_found", status_code=404)
        repo.update_user(
            self.db, user_id,
            password_hash=passwords.hash_password(new_password),
            password_changed_at=datetime.now(UTC).replace(microsecond=0).isoformat(),
            must_change_password=True,
        )
        repo.revoke_all_sessions(self.db, user_id, reason="admin_password_reset")
        self.audit.record(
            SecurityEventTypes.PASSWORD_RESET, outcome="ok",
            user_id=user_id, username=user.get("username"),
            detail={"actor_user_id": actor_id},
        )

    # -- verification ----------------------------------------------------
    def verify(self, claims: tokens.TokenClaims) -> dict | None:
        """Resolve the claims to a live user, or ``None`` if they are stale."""
        user = repo.get_user(self.db, claims.sub)
        if not user or not user.get("is_active"):
            return None
        return user

    def maybe_upgrade_hash(self, user: dict, password: str) -> bool:
        """Re-hash with current parameters when the policy has been raised."""
        if passwords.needs_rehash(user["password_hash"],
                                  n=self.config.scrypt_n, r=self.config.scrypt_r,
                                  p=self.config.scrypt_p):
            repo.update_user(
                self.db, int(user["id"]),
                password_hash=passwords.hash_password(
                    password, n=self.config.scrypt_n, r=self.config.scrypt_r,
                    p=self.config.scrypt_p, maxmem=self.config.scrypt_maxmem,
                ),
            )
            return True
        return False

    def prune(self, keep_days: int = 30) -> int:
        with self.db.write() as conn:
            conn.execute("DELETE FROM revoked_tokens WHERE expires_at <= datetime('now')")
        sessions = repo.prune_sessions(self.db, keep_days)
        attempts = self.lockout.prune(keep_days)
        return sessions + attempts


def _remembered(session: dict) -> bool:
    """A session created with "remember me" has a far-future expiry."""
    try:
        expires = datetime.fromisoformat(str(session["expires_at"]))
    except (TypeError, ValueError):
        return False
    now = datetime.now(UTC)
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=UTC)
    return (expires - now) > timedelta(days=2)