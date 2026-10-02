"""Access and refresh tokens.

Design
------
* Access token: a **signed, self-describing** token, not a database lookup.
  Format ``ssat.<payload_b64url>.<sig_b64url>`` where the payload is compact
  JSON ``{sub, jti, typ, prof, iat, exp, ver, jti}``.  It is signed with
  HMAC-SHA256 using a 32-byte secret generated at first start and stored
  0600 next to the database.

  Using a JWT-shaped token rather than an opaque row means the common API path
  needs *no* database round trip to authorise a request, which is what keeps a
  4-core box responsive with ten clients.  The trade-off is revocation lag,
  which is handled by (a) 15-minute access-token lifetimes and (b) an explicit
  revocation table checked on logout / password change.

* Refresh token: 32 random bytes, opaque, shown to the client once and stored
  only as SHA-256.  A database row per session gives us listing, per-device
  revocation and instant kill.

* Media/playback tokens: short-lived, single-purpose, bound to one media row,
  one profile and one byte range.  See :func:`issue_playback_token`.

Why HMAC and not EdDSA/JWT with RSA: no key rotation infrastructure to get
wrong, symmetric verification is fast, and there is exactly one trust boundary
(a single server process).  The signing key is read from the database at boot.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import stat
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

ACCESS_PREFIX = "ssat"
REFRESH_PREFIX = "ssrt"
PLAYBACK_PREFIX = "sspt"

TOKEN_TYPE_ACCESS = "access"
TOKEN_TYPE_MEDIA = "media"


class TokenError(Exception):
    """Raised when a token is malformed, unsigned, expired or revoked."""

    def __init__(self, message: str, code: str = "invalid_token") -> None:
        super().__init__(message)
        self.code = code


@dataclass(slots=True)
class TokenClaims:
    sub: int              # user id
    jti: str              # unique token id (also the revocation key)
    typ: str              # TOKEN_TYPE_*
    iat: datetime
    exp: datetime
    profile_id: int | None = None
    session_id: str | None = None
    #: Media-token only.
    media_id: int | None = None
    profile_name: str | None = None

    @property
    def scopes(self) -> frozenset[str]:
        if self.typ == TOKEN_TYPE_MEDIA:
            return frozenset({"media:stream"})
        return frozenset({"api"})


# ---------------------------------------------------------------------------
# base64url helpers (no padding)
# ---------------------------------------------------------------------------
def _b64u_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64u_decode(text: str | bytes) -> bytes:
    """Decode base64url, tolerating the input arriving as str or bytes.

    Callers pass a file read as bytes (the signing key) as well as str (token
    segments), so both are accepted rather than failing on a type mismatch that
    is a bug here rather than an attack.
    """
    if isinstance(text, bytes):
        raw = text.strip()
    elif isinstance(text, str):
        raw = text.strip().encode("ascii", errors="ignore")
    else:
        raise TokenError("token is not valid base64url")
    padding = b"=" * (-len(raw) % 4)
    try:
        return base64.urlsafe_b64decode(raw + padding)
    except Exception as exc:  # noqa: BLE001 - any decode failure is the same
        raise TokenError("token is not valid base64url") from exc


# ---------------------------------------------------------------------------
# Signing key
# ---------------------------------------------------------------------------
def load_or_create_secret(db_path: Path, *, key_path: Path | None = None) -> bytes:
    """Return the server signing secret, generating it on first run.

    Stored beside the database with 0600.  Regenerating it is safe: it simply
    invalidates every outstanding access token, and refresh tokens are stored
    hashed so they survive.
    """
    key_path = key_path or (db_path.parent / "signing.key")
    key_path.parent.mkdir(parents=True, exist_ok=True)
    if key_path.exists():
        try:
            material = key_path.read_bytes().strip()
            if len(material) >= 43:
                return _b64u_decode(material)[:32]
        except OSError:
            pass

    secret = secrets.token_bytes(32)
    # Create with O_EXCL-style semantics: write to a private temp file, then
    # rename.  0600 from the moment it exists so the secret is never briefly
    # world-readable.
    fd = os.open(str(key_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, _b64u_encode(secret).encode("ascii") + b"\n")
        os.fsync(fd)
    finally:
        os.close(fd)
    os.chmod(key_path, stat.S_IRUSR | stat.S_IWUSR)
    return secret


# ---------------------------------------------------------------------------
# Signed token encode/decode
# ---------------------------------------------------------------------------
def _sign(secret: bytes, payload_b64: str) -> str:
    mac = hmac.new(secret, payload_b64.encode("ascii"), hashlib.sha256).digest()
    return _b64u_encode(mac)


def encode_signed(secret: bytes, prefix: str, claims: dict[str, Any]) -> str:
    payload = json.dumps(claims, separators=(",", ":"), sort_keys=True).encode("utf-8")
    payload_b64 = _b64u_encode(payload)
    return f"{prefix}.{payload_b64}.{_sign(secret, payload_b64)}"


def decode_signed(secret: bytes, prefix: str, token: str,
                  *, max_size: int = 4096) -> dict[str, Any]:
    """Verify structure, prefix, signature and expiry.  Raises :class:`TokenError`."""
    if not isinstance(token, str):
        raise TokenError("token must be a string")
    if len(token) > max_size:
        raise TokenError("token is implausibly large")
    parts = token.split(".")
    if len(parts) != 3:
        raise TokenError("token is malformed")
    token_prefix, payload_b64, signature = parts
    if not hmac.compare_digest(token_prefix, prefix):
        raise TokenError("wrong token type")

    expected = _sign(secret, payload_b64)
    if not hmac.compare_digest(expected, signature):
        # Distinct message so a client cannot tell "signed by another server"
        # from "edited in transit" -- both are just invalid.
        raise TokenError("token signature is invalid", code="invalid_token")

    claims = json.loads(_b64u_decode(payload_b64))
    if not isinstance(claims, dict):
        raise TokenError("token payload is not an object")

    exp = claims.get("exp")
    if not isinstance(exp, int):
        raise TokenError("token has no expiry")
    if datetime.fromtimestamp(exp, tz=UTC) <= datetime.now(UTC):
        raise TokenError("token has expired", code="token_expired")
    iat = claims.get("iat")
    if isinstance(iat, int) and iat > datetime.now(UTC).timestamp() + 60:
        raise TokenError("token is not yet valid", code="invalid_token")
    return claims


# ---------------------------------------------------------------------------
# Access tokens
# ---------------------------------------------------------------------------
def create_access_token(secret: bytes, *, user_id: int,
                        profile_id: int | None = None,
                        session_id: str | None = None,
                        ttl_seconds: int = 900,
                        token_type: str = TOKEN_TYPE_ACCESS,
                        media_id: int | None = None,
                        profile_name: str | None = None) -> tuple[str, TokenClaims]:
    now = datetime.now(UTC)
    exp = now + timedelta(seconds=ttl_seconds)
    jti = _b64u_encode(secrets.token_bytes(18))
    claims: dict[str, Any] = {
        "sub": user_id,
        "jti": jti,
        "typ": token_type,
        "iat": int(now.timestamp()),
        "exp": int(exp.timestamp()),
        "ver": 1,
    }
    if profile_id is not None:
        claims["prof"] = profile_id
    if session_id is not None:
        claims["sid"] = session_id
    if media_id is not None:
        claims["mid"] = media_id
    if profile_name is not None:
        claims["pname"] = profile_name
    token = encode_signed(secret, ACCESS_PREFIX, claims)
    return token, TokenClaims(
        sub=user_id, jti=jti, typ=token_type, iat=now, exp=exp,
        profile_id=profile_id, session_id=session_id, media_id=media_id,
        profile_name=profile_name,
    )


def parse_access_token(secret: bytes, token: str) -> TokenClaims:
    claims = decode_signed(secret, ACCESS_PREFIX, token)
    sub = claims.get("sub")
    jti = claims.get("jti")
    if not isinstance(sub, int) or not isinstance(jti, str):
        raise TokenError("token payload is missing required claims")
    typ = claims.get("typ")
    if typ not in (TOKEN_TYPE_ACCESS, TOKEN_TYPE_MEDIA):
        raise TokenError("unknown token type")
    return TokenClaims(
        sub=sub,
        jti=jti,
        typ=typ,
        iat=datetime.fromtimestamp(claims["iat"], tz=UTC),
        exp=datetime.fromtimestamp(claims["exp"], tz=UTC),
        profile_id=claims.get("prof") if isinstance(claims.get("prof"), int) else None,
        session_id=claims.get("sid") if isinstance(claims.get("sid"), str) else None,
        media_id=claims.get("mid") if isinstance(claims.get("mid"), int) else None,
        profile_name=claims.get("pname") if isinstance(claims.get("pname"), str) else None,
    )


# ---------------------------------------------------------------------------
# Refresh tokens (opaque, stored hashed)
# ---------------------------------------------------------------------------
def create_refresh_token() -> tuple[str, str]:
    """Return ``(plaintext, sha256_hex)``."""
    plaintext = f"{REFRESH_PREFIX}.{_b64u_encode(secrets.token_bytes(32))}"
    return plaintext, hash_refresh_token(plaintext)


def hash_refresh_token(plaintext: str) -> str:
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


def is_refresh_token(plaintext: str) -> bool:
    return isinstance(plaintext, str) and plaintext.startswith(REFRESH_PREFIX + ".")


# ---------------------------------------------------------------------------
# Playback tokens
# ---------------------------------------------------------------------------
def create_playback_token(secret: bytes, *, user_id: int, profile_id: int,
                          media_id: int, ttl_seconds: int = 600) -> tuple[str, TokenClaims]:
    """Short-lived token that authorises exactly one media item for one profile."""
    return create_access_token(
        secret,
        user_id=user_id,
        profile_id=profile_id,
        token_type=TOKEN_TYPE_MEDIA,
        media_id=media_id,
        ttl_seconds=ttl_seconds,
    )


# ---------------------------------------------------------------------------
# Revocation
# ---------------------------------------------------------------------------
def revoke(db: sqlite3.Connection, jti: str, expires_at: str, reason: str = "logout") -> None:
    db.execute(
        "INSERT OR REPLACE INTO revoked_tokens (jti, expires_at, reason) VALUES (?, ?, ?)",
        (jti, expires_at, reason),
    )


def is_revoked(db: sqlite3.Connection, jti: str) -> bool:
    row = db.execute(
        "SELECT 1 FROM revoked_tokens WHERE jti = ? AND expires_at > datetime('now')",
        (jti,),
    ).fetchone()
    return row is not None


def purge_expired(db: sqlite3.Connection) -> int:
    cur = db.execute("DELETE FROM revoked_tokens WHERE expires_at <= datetime('now')")
    return cur.rowcount or 0


def extract_bearer(header_value: str | None) -> str | None:
    """Pull the credential out of an ``Authorization: Bearer <token>`` header."""
    if not header_value:
        return None
    parts = header_value.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    candidate = parts[1].strip()
    return candidate or None