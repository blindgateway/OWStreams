"""Audit sink for the logging framework.

Writes a structured, human-readable mirror of every audit record to a daily
log file so the trail can be shipped off-box without touching SQLite.

Redaction is applied twice, deliberately:

* :mod:`streamserver.security.audit` scrubs the *detail payload* it stores.
* this formatter scrubs the *formatted line* itself.

The second pass matters because the audit logger also emits interpolated
messages, and a bug in a call site that passes a token as ``username`` would
otherwise be written straight to disk.
"""

from __future__ import annotations

import logging
import re
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

from .audit import _VALUE_PATTERNS

#: Messages the audit logger emits that are not "audit action=..." records.
REDACT_REPLACEMENT = "<redacted>"

#: Bearer tokens, scrypt hashes, refresh tokens and playback tokens.
_PATTERNS = tuple(_VALUE_PATTERNS) + (
    # Authorization header values in a request line dump.
    re.compile(r"(?i)\bauthorization:\s*\S+"),
    re.compile(r"(?i)\b(cookie|set-cookie):\s*[^\r\n]+"),
    # JSON-ish password fields.
    re.compile(r'(?i)("?(?:password|pin|token|secret)"?\s*[:=]\s*)("?)[^\s,;"}]+'),
)


def scrub_text(text: str) -> str:
    """Remove anything credential-shaped from a single line of log output."""
    for pattern in _PATTERNS:
        text = pattern.sub(
            lambda m: (m.group(1) + REDACT_REPLACEMENT) if m.lastindex else REDACT_REPLACEMENT,
            text,
        )
    return text


class RedactingFormatter(logging.Formatter):
    """Formatter that scrubs every rendered record."""

    def format(self, record: logging.LogRecord) -> str:
        return scrub_text(super().format(record))


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        # Scrub the raw args too, so a handler that formats differently (or a
        # crash handler that dumps ``record.msg``) still cannot leak them.
        if isinstance(record.args, dict):
            record.args = {k: _scrub_value(v) for k, v in record.args.items()}
        elif isinstance(record.args, tuple):
            record.args = tuple(_scrub_value(a) for a in record.args)
        if isinstance(record.msg, str):
            record.msg = scrub_text(record.msg)
        return True


def _scrub_value(value):
    if isinstance(value, str):
        return scrub_text(value)
    if isinstance(value, dict):
        return {k: ("<redacted>" if _sensitive(k) else _scrub_value(v)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return type(value)(_scrub_value(v) for v in value)
    return value


def _sensitive(key) -> bool:
    key = str(key).lower()
    return any(word in key for word in
               ("password", "passwd", "pin", "token", "secret", "authorization",
                "cookie", "credential"))


class AuditFileHandler(TimedRotatingFileHandler):
    """Daily-rotating audit log with 0600 permissions from creation."""

    def __init__(self, path: Path, *, days: int = 180) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        super().__init__(
            str(path),
            when="midnight",
            backupCount=max(1, days),
            encoding="utf-8",
            delay=False,
        )
        self.setFormatter(
            RedactingFormatter("%(asctime)s %(levelname)s %(name)s %(message)s")
        )
        self.addFilter(RedactingFilter())
        _harden(path)
        for existing in sorted(path.parent.glob(f"{path.name}.*")):
            _harden(existing)


def _harden(path: Path) -> None:
    try:
        path.chmod(0o600)
    except OSError:
        pass