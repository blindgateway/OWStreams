"""Security primitives: password/PIN hashing, tokens, path containment,
rate limiting and the audit trail."""

from .passwords import (
    hash_password,
    hash_pin,
    needs_rehash,
    pin_strength,
    verify_password,
    verify_pin,
)
from .paths import MediaRootRegistry, PathViolation, sanitize_filename, slugify
from .tokens import (
    TokenClaims,
    TokenError,
    create_access_token,
    create_playback_token,
    create_refresh_token,
    extract_bearer,
    hash_refresh_token,
    load_or_create_secret,
    parse_access_token,
)

__all__ = [
    "hash_password", "hash_pin", "needs_rehash", "pin_strength",
    "verify_password", "verify_pin",
    "MediaRootRegistry", "PathViolation", "sanitize_filename", "slugify",
    "TokenClaims", "TokenError", "create_access_token", "create_playback_token",
    "create_refresh_token", "extract_bearer", "hash_refresh_token",
    "load_or_create_secret", "parse_access_token",
]
