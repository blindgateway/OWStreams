"""Password and PIN hashing."""

from __future__ import annotations

import re
import time

import pytest

from streamserver.security import passwords


def _b64(raw: bytes) -> str:
    """base64 the way security/passwords.py does, for building test vectors."""
    import base64

    return base64.b64encode(raw).decode("ascii")


# ---------------------------------------------------------------------------
# Passwords
# ---------------------------------------------------------------------------
def test_hash_is_self_describing_and_verifies():
    encoded = passwords.hash_password("correct horse battery staple", n=1024)
    assert encoded.startswith("scrypt$")
    parts = encoded.split("$")
    assert len(parts) == 6
    assert parts[1] == "1024"
    assert passwords.verify_password("correct horse battery staple", encoded)


def test_wrong_password_is_rejected():
    encoded = passwords.hash_password("right", n=1024)
    assert not passwords.verify_password("wrong", encoded)
    assert not passwords.verify_password("Right", encoded)
    assert not passwords.verify_password("right ", encoded)


def test_password_is_never_recoverable_from_the_hash():
    encoded = passwords.hash_password("swordfish-1234", n=1024)
    assert "swordfish" not in encoded
    assert "1234" not in encoded


def test_same_password_gets_a_different_salt_each_time():
    first = passwords.hash_password("same", n=1024)
    second = passwords.hash_password("same", n=1024)
    assert first != second
    assert passwords.verify_password("same", first)
    assert passwords.verify_password("same", second)


@pytest.mark.parametrize("broken", [
    "", None, "not-a-hash", "scrypt$1$2$3", "bcrypt$1024$8$1$aaaa$bbbb",
    "scrypt$0$8$1$c2FsdA==$aGFzaA==",             # n must be >= 2 and a power of 2
    "scrypt$1024$8$1$c2FsdA==$c2hvcnQ=",         # key too short
    # n above the ceiling: a corrupt row must be rejected rather than be
    # allowed to demand ~1 GiB of memory on every login attempt.
    f"scrypt$1048576$8$1${_b64(b'salt')}${_b64(b'x' * 64)}",
])
def test_malformed_hashes_never_verify(broken):
    """A corrupt row must fail closed, not raise, and not authenticate."""
    assert not passwords.verify_password("anything", broken)
    assert not passwords.verify_password("", broken)


def test_malformed_hash_is_flagged_for_rehash():
    assert passwords.needs_rehash("garbage")
    assert passwords.needs_rehash("")
    assert passwords.needs_rehash(None)


def test_needs_rehash_tracks_policy_changes():
    weak = passwords.hash_password("x" * 12, n=1024)
    assert passwords.needs_rehash(weak, n=1024) is False
    assert passwords.needs_rehash(weak, n=2048) is True


def test_hash_travels_across_parameter_changes():
    """An old hash still verifies after the policy is tightened."""
    old = passwords.hash_password("still works", n=1024)
    assert passwords.verify_password("still works", old)
    assert passwords.needs_rehash(old, n=32768)


def test_unicode_passwords_normalise_consistently():
    """NFC and NFD spellings of the same string must not be two passwords."""
    composed = passwords.hash_password("café-münchen-2024", n=1024)
    decomposed = passwords.hash_password("café-münchen-2024", n=1024)
    assert passwords.verify_password("café-münchen-2024", composed)
    assert passwords.verify_password("café-münchen-2024", decomposed)


def test_extremely_long_input_is_bounded():
    """A multi-megabyte password must not become a CPU denial of service."""
    started = time.monotonic()
    encoded = passwords.hash_password("a" * 5_000_000, n=1024)
    assert passwords.verify_password("a" * 5_000_000, encoded)
    assert time.monotonic() - started < 5.0


def test_hash_rejects_invalid_parameters():
    with pytest.raises(ValueError, match="power of two"):
        passwords.hash_password("x", n=1000)
    with pytest.raises(ValueError, match="r="):
        passwords.hash_password("x", n=1024, r=0)


def test_parse_hash_round_trip():
    encoded = passwords.hash_password("round trip", n=2048)
    n, r, p, salt, key = passwords.parse_hash(encoded)
    assert (n, r, p) == (2048, 8, 1)
    assert len(salt) == passwords.DEFAULT_SALT_BYTES
    assert len(key) == passwords.DEFAULT_KEY_BYTES


# ---------------------------------------------------------------------------
# PINs
# ---------------------------------------------------------------------------
def test_pin_hash_verifies():
    encoded = passwords.hash_pin("1234")
    assert encoded.startswith("scrypt$")
    assert passwords.verify_pin("1234", encoded)
    assert not passwords.verify_pin("1235", encoded)


def test_pin_is_never_stored_in_plaintext():
    encoded = passwords.hash_pin("4821")
    assert "4821" not in encoded


def test_pin_rejects_letters_and_wrong_length():
    for bad in ("abcd", "123", "123456789", "", "12 34"):
        with pytest.raises(ValueError):
            passwords.hash_pin(bad)


def test_pin_with_no_stored_hash_never_verifies():
    assert not passwords.verify_pin("1234", None)


def test_legacy_plaintext_pin_is_accepted_once():
    """A pre-1.0 database stored PINs verbatim; the service upgrades them."""
    assert passwords.verify_pin("1234", "1234")
    assert not passwords.verify_pin("9999", "1234")


@pytest.mark.parametrize("pin,score", [
    ("1234", 3),      # sequential, all distinct
    ("1111", 2),      # all identical
    ("4321", 3),      # descending is still sequential, so it is penalised
    ("1357", 4),      # four distinct, not sequential
])
def test_pin_strength_discourages_obvious_patterns(pin, score):
    assert passwords.pin_strength(pin) == score


def test_pin_strength_rejects_non_numeric():
    assert passwords.pin_strength("abcd") == 0


def test_random_password_is_usable_and_unique():
    first = passwords.random_password(24)
    second = passwords.random_password(24)
    assert len(first) == 24
    assert first != second
    # No characters that are easy to confuse when copied off a screen.
    assert not set(first) & set("Il1O0o")


def test_constant_time_equals():
    assert passwords.constant_time_equals("abc", "abc")
    assert not passwords.constant_time_equals("abc", "abd")
    assert not passwords.constant_time_equals("abc", "abcde")
    assert not passwords.constant_time_equals("", "a")
    assert not passwords.constant_time_equals(None, "a")


def test_hash_format_is_regex_parseable():
    """The encoding is documented; lock the shape so a future change is loud."""
    encoded = passwords.hash_password("shape", n=1024)
    assert re.fullmatch(
        r"scrypt\$\d+\$\d+\$\d+\$[A-Za-z0-9+/=]+\$[A-Za-z0-9+/=]+", encoded
    ), encoded