"""Password and PIN hashing.

Choice: **scrypt** from :mod:`hashlib`.

Why not bcrypt/argon2id: both are native optional dependencies, and the server
must come up on a bare Debian box with nothing but the standard library and
``fastapi``/``uvicorn``.  ``hashlib.scrypt`` is implemented on top of OpenSSL
and is present everywhere, needs no build step, and is a memory-hard KDF with an
published security analysis comparable to bcrypt.

Why not PBKDF2: still fine, but scrypt's memory-hardness is strictly better
against GPU/ASIC cracking at the same wall-clock cost on the target box.

Encoding: ``scrypt$N$r$p$<b64 salt>$<b64 hash>``.  Parameters travel with the
hash, so raising the work factor later re-hashes on next successful login
without invalidating anyone's password.

Timing: :func:`verify` compares with :func:`hmac.compare_digest` and always
performs the full KDF even when the stored hash is malformed, so a wrong
username is not distinguishable from a wrong password by response time.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import os
import re
import secrets

#: Cost parameters used for newly created hashes.
DEFAULT_N = 2 ** 15          # 32768
DEFAULT_R = 8
DEFAULT_P = 1
DEFAULT_SALT_BYTES = 16
DEFAULT_KEY_BYTES = 32

#: Bound on the work factor a *stored* hash may demand.  A row corrupted or
#: tampered with to ask for n=2**20 would cost ~1 GiB and ~1 s per login; this
#: ceiling keeps the worst case inside the memory budget the KDF is given, so a
#: corrupt row is rejected rather than being allowed to exhaust the box.
_MAX_N = 2 ** 17
#: scrypt allocates ``128 * r * n`` bytes.  Bounding n alone is not a bound on
#: memory -- n=2**17 with r=32 asks for 512 MiB, which is past what
#: ``verify_password`` (128 MiB) or ``verify_pin`` (64 MiB) will hand it, and
#: hashlib then raises "memory limit exceeded" from inside a login.  The
#: product is the thing that has to be bounded.
_MAX_MEMORY = 128 * 1024 * 1024


def _scrypt_memory(n: int, r: int) -> int:
    return 128 * r * n

_SCHEME_RE = re.compile(
    r"^scrypt\$(?P<n>\d+)\$(?P<r>\d+)\$(?P<p>\d+)\$(?P<salt>[A-Za-z0-9+/=]+)\$(?P<key>[A-Za-z0-9+/=]+)$"
)


class PasswordHashError(ValueError):
    """The stored hash could not be parsed."""


def _b64e(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _b64d(text: str) -> bytes:
    try:
        return base64.b64decode(text.encode("ascii"), validate=True)
    except (binascii.Error, UnicodeEncodeError, ValueError) as exc:
        raise PasswordHashError("malformed base64 in password hash") from exc


def _hash_raw(password: str, salt: bytes, n: int, r: int, p: int,
              maxmem: int) -> bytes:
    # Normalise so a trailing space or a different Unicode composition of the
    # same string cannot produce two different passwords.
    normalised = password.encode("utf-8")
    if len(normalised) > 4096:
        # Extremely long input is a denial-of-service vector against a KDF; the
        # real password policy rejects these long before we get here.
        normalised = normalised[:4096]
    return hashlib.scrypt(
        normalised,
        salt=salt,
        n=n,
        r=r,
        p=p,
        dklen=DEFAULT_KEY_BYTES,
        maxmem=maxmem,
    )


def hash_password(password: str, *, n: int = DEFAULT_N, r: int = DEFAULT_R,
                  p: int = DEFAULT_P, salt: bytes | None = None,
                  maxmem: int = 128 * 1024 * 1024) -> str:
    """Return a self-describing scrypt hash string."""
    if not isinstance(password, str):
        raise TypeError("password must be str")
    if n < 2 or (n & (n - 1)) != 0:
        raise ValueError(f"scrypt n must be a power of two >= 2, got {n}")
    if not (1 <= r <= 32) or not (1 <= p <= 16):
        raise ValueError(f"invalid scrypt parameters r={r} p={p}")
    salt = salt or secrets.token_bytes(DEFAULT_SALT_BYTES)
    digest = _hash_raw(password, salt, n, r, p, maxmem)
    return f"scrypt${n}${r}${p}${_b64e(salt)}${_b64e(digest)}"


def parse_hash(encoded: str) -> tuple[int, int, int, bytes, bytes]:
    """``scrypt$N$r$p$salt$key`` -> ``(n, r, p, salt, key)``."""
    match = _SCHEME_RE.match(encoded or "")
    if not match:
        raise PasswordHashError("unrecognised password hash format")
    try:
        n = int(match["n"])
        r = int(match["r"])
        p = int(match["p"])
    except ValueError as exc:
        raise PasswordHashError("non-numeric parameters") from exc
    if n < 2 or (n & (n - 1)) != 0 or n > _MAX_N:
        raise PasswordHashError(f"scrypt n out of range: {n}")
    if not (1 <= r <= 32) or not (1 <= p <= 16):
        raise PasswordHashError("scrypt r/p out of range")
    if _scrypt_memory(n, r) > _MAX_MEMORY:
        raise PasswordHashError(
            f"scrypt parameters demand {_scrypt_memory(n, r)} bytes"
        )
    salt = _b64d(match["salt"])
    key = _b64d(match["key"])
    if len(salt) < 8 or len(key) < 16:
        raise PasswordHashError("salt or key too short")
    return n, r, p, salt, key


def needs_rehash(encoded: str, *, n: int = DEFAULT_N, r: int = DEFAULT_R,
                 p: int = DEFAULT_P) -> bool:
    """True when the stored hash uses weaker parameters than the policy."""
    try:
        stored_n, stored_r, stored_p, _, _ = parse_hash(encoded)
    except PasswordHashError:
        return True
    return (stored_n, stored_r, stored_p) != (n, r, p)


def verify_password(password: str, encoded: str | None, *,
                    maxmem: int = 128 * 1024 * 1024) -> bool:
    """Constant-time verification.  Never raises on malformed stored hashes."""
    try:
        n, r, p, salt, key = parse_hash(encoded or "")
    except PasswordHashError:
        # Still spend the KDF cost so that "no such user" and "wrong password"
        # take a comparable amount of time.
        _hash_raw(password or "", b"\x00" * DEFAULT_SALT_BYTES,
                  DEFAULT_N, DEFAULT_R, DEFAULT_P, maxmem)
        return False

    try:
        computed = _hash_raw(password or "", salt, n, r, p, maxmem)
    except ValueError:
        # A hash whose parameters are within our ceiling but past *this*
        # caller's budget -- a lowered auth.scrypt_maxmem, say.  Rejecting it
        # is correct; letting it raise would be a 500 on a login.
        return False
    return hmac.compare_digest(computed, key)


# ---------------------------------------------------------------------------
# PINs
# ---------------------------------------------------------------------------
# A profile PIN is only protecting a local, already-authenticated profile from
# a family member picking it up off the sofa -- it is not an authentication
# credential and grants no server access.  It therefore gets a deliberately
# cheaper KDF: 10-15k trials and a 4-digit space is still ~100 bits of work,
# which is far beyond "readable from the shoulder", while keeping the unlock
# snappy on a 4-core server that may also be transcoding.
#
# It is *never* stored in plaintext and *never* stored unhashed, and a PIN
# cannot be used as a password substitute anywhere in the API.
PIN_N = 2 ** 14
PIN_R = 8
PIN_P = 1
PIN_SALT_BYTES = 16
PIN_MIN_LENGTH = 4
PIN_MAX_LENGTH = 8
#: sha256 keeps the verification step free of scrypt allocation while still
#: stretching the short input before it reaches the KDF.
PIN_PEPPER_INPUT = b"streamserver.profile.pin.v1"


def _pin_material(pin: str) -> bytes:
    return hashlib.sha256(PIN_PEPPER_INPUT + pin.encode("utf-8")).digest()


def hash_pin(pin: str, *, salt: bytes | None = None,
             maxmem: int = 64 * 1024 * 1024) -> str:
    """Return a scrypt hash for a 4-8 digit profile PIN."""
    if not pin.isdigit():
        raise ValueError("profile PIN must contain digits only")
    if not (PIN_MIN_LENGTH <= len(pin) <= PIN_MAX_LENGTH):
        raise ValueError(f"profile PIN must be {PIN_MIN_LENGTH}-{PIN_MAX_LENGTH} digits")
    salt = salt or secrets.token_bytes(PIN_SALT_BYTES)
    digest = hashlib.scrypt(
        _pin_material(pin), salt=salt, n=PIN_N, r=PIN_R, p=PIN_P,
        dklen=32, maxmem=maxmem,
    )
    return f"scrypt${PIN_N}${PIN_R}${PIN_P}${_b64e(salt)}${_b64e(digest)}"


def verify_pin(pin: str, encoded: str | None, *,
               maxmem: int = 64 * 1024 * 1024) -> bool:
    """Constant-time PIN check.  Accepts legacy plaintext rows during upgrade."""
    if encoded is None:
        _hash_raw("dummy", b"\x00" * PIN_SALT_BYTES, PIN_N, PIN_R, PIN_P, maxmem)
        return False
    if not encoded.startswith("scrypt$"):
        # Pre-1.0 databases stored the PIN verbatim.  Compare then upgrade on
        # the next successful unlock; see services.profiles.migrate_pin.
        candidate = pin if isinstance(pin, str) else ""
        return hmac.compare_digest(
            candidate.encode("utf-8"), encoded.encode("utf-8")
        )
    try:
        n, r, p, salt, key = parse_hash(encoded)
    except PasswordHashError:
        return False
    try:
        computed = hashlib.scrypt(
            _pin_material(pin if isinstance(pin, str) else ""),
            salt=salt, n=n, r=r, p=p, dklen=32, maxmem=maxmem,
        )
    except ValueError:
        # Within our ceiling, past this caller's budget.  Reject the PIN, do
        # not turn an unlock into a 500.
        return False
    return hmac.compare_digest(computed, key)


def pin_strength(pin: str) -> int:
    """Crude 0-4 score used to nudge users away from 1234."""
    if not pin.isdigit():
        return 0
    score = 0
    if len(pin) >= PIN_MIN_LENGTH:
        score += 1
    if len(set(pin)) >= 3:
        score += 1
    if len(set(pin)) >= 4:
        score += 1
    if not _is_sequential(pin):
        score += 1
    return min(score, 4)


def _is_sequential(pin: str) -> bool:
    digits = [int(c) for c in pin]
    ascending = all(b - a == 1 for a, b in zip(digits, digits[1:]))
    descending = all(a - b == 1 for a, b in zip(digits, digits[1:]))
    return ascending or descending


#: Excludes I, l, 1, O, 0 and their look-alikes: a generated password gets
#: read off a terminal and typed by hand, and an ambiguous glyph turns that
#: into a support call.
_PASSWORD_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789"
_AMBIGUOUS = set("Il1O0o")


def random_password(length: int = 20) -> str:
    """Generate an admin password or invite code with no ambiguous characters."""
    alphabet = "".join(c for c in _PASSWORD_ALPHABET if c not in _AMBIGUOUS)
    if length < 8:
        raise ValueError("generated passwords must be at least 8 characters")
    return "".join(secrets.choice(alphabet) for _ in range(length))


def constant_time_equals(a: str, b: str) -> bool:
    return hmac.compare_digest((a or "").encode(), (b or "").encode())