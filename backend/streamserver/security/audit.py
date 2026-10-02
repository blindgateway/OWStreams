"""Security audit trail.

What belongs here: *who did what, to which object, from where, and did it
succeed.*  What must never appear: passwords, PINs, tokens, request bodies,
filenames of un-imported media, or any personal viewing content beyond the
action name.

The trail is written to SQLite (queryable by the API) and mirrored to a
rotating log file so an operator can ship it off-box without touching the
database.  Writes are best-effort: an audit failure must never break a request,
but it is logged loudly to stderr.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from typing import Any

log = logging.getLogger("streamserver.audit")

#: Keys whose values are scrubbed wherever they appear in a detail mapping.
_SENSITIVE_KEYS = re.compile(
    r"(pass|password|pin|token|secret|authorization|cookie|credential|otp|"
    r"refresh|access|bearer|session[_-]?id|media_path|path)",
    re.IGNORECASE,
)

#: Anything that looks like a bearer token or a scrypt hash is redacted even if
#: it arrives under an innocuous key.
_VALUE_PATTERNS = (
    re.compile(r"\bssat\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+"),
    re.compile(r"\bssrt\.[A-Za-z0-9_\-]+"),
    re.compile(r"\bsspt\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+"),
    re.compile(r"\bscrypt\$\d+\$\d+\$\d+\$[A-Za-z0-9+/=]+\$[A-Za-z0-9+/=]+"),
)

MAX_DETAIL_KEYS = 24
MAX_DETAIL_VALUE = 200


def _scrub(value: Any, depth: int = 0) -> Any:
    if depth > 4:
        return "<truncated>"
    if isinstance(value, str):
        out = value[:MAX_DETAIL_VALUE]
        for pattern in _VALUE_PATTERNS:
            out = pattern.sub("<redacted>", out)
        return out
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= MAX_DETAIL_KEYS:
                result["_truncated"] = True
                break
            key_str = str(key)
            if _SENSITIVE_KEYS.search(key_str):
                result[key_str] = "<redacted>"
            else:
                result[key_str] = _scrub(item, depth + 1)
        return result
    if isinstance(value, (list, tuple, set)):
        return [_scrub(item, depth + 1) for item in list(value)[:MAX_DETAIL_KEYS]]
    return _scrub(str(value), depth + 1)


class AuditLogger:
    """Append-only audit writer.  Safe to call from request handlers."""

    def __init__(self, db, *, enabled: bool = True, file_logger: logging.Logger | None = None):
        self.db = db
        self.enabled = enabled
        self.file_logger = file_logger or log
        self._lock = threading.Lock()

    def record(
        self,
        action: str,
        *,
        outcome: str = "ok",
        user_id: int | None = None,
        username: str | None = None,
        profile_id: int | None = None,
        media_id: int | None = None,
        ip_address: str | None = None,
        device_name: str | None = None,
        request_id: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        if not self.enabled:
            return
        if outcome not in {"ok", "denied", "error"}:
            outcome = "error"

        # Fold the media id into the detail payload before it is serialised, so
        # it is actually stored and not only printed to the log line.
        if media_id is not None:
            detail = dict(detail or {})
            detail.setdefault("media_id", int(media_id))

        # Fold the media id into the detail payload before it is serialised, so
        # it is actually stored and not only printed to the log line.
        if media_id is not None:
            detail = dict(detail or {})
            detail.setdefault("media_id", int(media_id))

        scrubbed = _scrub(detail or {})
        payload = json.dumps(scrubbed, separators=(",", ":"), sort_keys=True)

        try:
            with self._lock:
                self.db.execute(
                    "INSERT INTO audit_log "
                    "(user_id, username, profile_id, action, outcome, ip_address, "
                    " device_name, detail, request_id) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        user_id,
                        (username or "")[:64] or None,
                        profile_id,
                        action[:64],
                        outcome,
                        _normalise_ip(ip_address),
                        (device_name or "")[:128] or None,
                        payload,
                        (request_id or "")[:64] or None,
                    ),
                )
        except Exception:  # noqa: BLE001 - auditing must never break a request
            self.file_logger.exception("failed to write audit record action=%s", action)
            return

        self.file_logger.info(
            "audit action=%s outcome=%s user=%s profile=%s media=%s ip=%s detail=%s",
            action, outcome, username or "-", profile_id or "-", media_id or "-",
            _normalise_ip(ip_address) or "-", payload,
        )

    def recent(self, *, limit: int = 100, user_id: int | None = None,
               action: str | None = None) -> list[dict[str, Any]]:
        clauses, params = [], []
        if user_id is not None:
            clauses.append("user_id = ?")
            params.append(user_id)
        if action:
            clauses.append("action LIKE ?")
            params.append(f"%{action}%")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(max(1, min(limit, 1000)))
        rows = self.db.query(
            f"SELECT * FROM audit_log {where} ORDER BY id DESC LIMIT ?", params
        )
        return [_row_to_dict(row) for row in rows]

    def prune(self, retention_days: int) -> int:
        result = self.db.execute(
            "DELETE FROM audit_log WHERE at < datetime('now', ?)",
            (f"-{max(1, retention_days)} days",),
        )
        return result.changes

    def count_recent(self, *, hours: int = 24, outcome: str | None = None) -> int:
        if outcome:
            row = self.db.query_one(
                "SELECT COUNT(*) FROM audit_log WHERE at > datetime('now', ?) AND outcome = ?",
                (f"-{hours} hours", outcome),
            )
        else:
            row = self.db.query_one(
                "SELECT COUNT(*) FROM audit_log WHERE at > datetime('now', ?)",
                (f"-{hours} hours",),
            )
        return int(row[0]) if row else 0


def _row_to_dict(row) -> dict[str, Any]:
    data = dict(row)
    if data.get("detail"):
        try:
            data["detail"] = json.loads(data["detail"])
        except (ValueError, TypeError):
            data["detail"] = {}
    return data


def _normalise_ip(ip: str | None) -> str | None:
    """Trim and validate an address before it reaches the log.

    IPv6 zone ids and unix sockets would otherwise let a hostile client inject
    newlines or megabytes into the audit table via the ``X-Forwarded-For``
    header.
    """
    if not ip:
        return None
    value = ip.strip()
    if len(value) > 45:
        return value[:45]
    if any(ch in value for ch in "\r\n\t\0 "):
        return None
    return value


class SecurityEventTypes:
    """Canonical action names -- keeps ``action LIKE`` queries meaningful."""

    LOGIN_SUCCESS = "auth.login.success"
    LOGIN_FAILURE = "auth.login.failure"
    LOGIN_LOCKED = "auth.login.locked"
    LOGOUT = "auth.logout"
    TOKEN_REFRESH = "auth.token.refresh"
    TOKEN_REJECTED = "auth.token.rejected"
    PASSWORD_CHANGED = "account.password.changed"
    PASSWORD_RESET = "account.password.reset"
    PROFILE_PIN_SET = "profile.pin.set"
    PROFILE_PIN_FAILED = "profile.pin.failed"
    PROFILE_SELECTED = "profile.selected"
    SESSION_REVOKED = "account.session.revoked"
    MEDIA_STREAM_START = "media.stream.start"
    MEDIA_STREAM_DENIED = "media.stream.denied"
    MEDIA_MISSING = "media.file.missing"
    IMPORT_RUN = "library.import.run"
    RATE_LIMITED = "security.rate_limited"
    ADMIN_ACTION = "admin.action"