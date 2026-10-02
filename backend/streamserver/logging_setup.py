"""Logging configuration.

Two streams with different jobs:

* **Application log** -- operational: startup, imports, scan jobs, errors.
  Rotating, 0600, scrubbed.
* **Audit log** -- security events.  Separate file so it can be retained on a
  different schedule and shipped without the noisy application chatter.

Access logs come from uvicorn and are deliberately *not* logged at INFO for
every media byte range: a 2-hour film issues thousands of Range requests and
logging each one would fill the disk and bury everything else.  Request logging
is instead aggregated into one line per client session (see middleware).
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path

from .security.audit_sink import (
    AuditFileHandler,
    RedactingFilter,
    RedactingFormatter,
    scrub_text,
)

_FORMAT = "%(asctime)s %(levelname)-7s %(name)-28s %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"

#: Third-party loggers that are noisy and not useful at INFO.
_QUIET = {
    "uvicorn.access": logging.WARNING,
    "uvicorn.error": logging.INFO,
    "uvicorn": logging.INFO,
    "multipart": logging.WARNING,
    "asyncio": logging.WARNING,
    "httpcore": logging.WARNING,
    "hpack": logging.WARNING,
    "watchfiles": logging.WARNING,
}


class _MaxLevelFilter(logging.Filter):
    def __init__(self, level: int) -> None:
        super().__init__()
        self._level = level

    def filter(self, record: logging.LogRecord) -> bool:
        return record.levelno < self._level


def setup_logging(
    log_dir: Path,
    *,
    level: str = "INFO",
    to_file: bool = True,
    app_log_name: str = "streamserver.log",
    audit_log_name: str = "audit.log",
    retention_days: int = 30,
    console: bool = True,
) -> dict[str, logging.Logger]:
    """Install handlers and return the configured loggers by name."""
    numeric_level = getattr(logging, str(level).upper(), logging.INFO)
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)

    formatter = RedactingFormatter(_FORMAT, datefmt=_DATEFMT)
    redactor = RedactingFilter()

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    for handler in list(root.handlers):
        root.removeHandler(handler)

    stream_handler = logging.StreamHandler(sys.stderr)
    stream_handler.setFormatter(formatter)
    stream_handler.addFilter(redactor)
    stream_handler.setLevel(numeric_level)
    if console:
        root.addHandler(stream_handler)

    app_handler: logging.Handler | None = None
    if to_file:
        app_handler = logging.handlers.TimedRotatingFileHandler(
            str(log_dir / app_log_name),
            when="midnight", backupCount=max(1, retention_days), encoding="utf-8",
        )
        app_handler.setFormatter(formatter)
        app_handler.addFilter(redactor)
        app_handler.addFilter(_MaxLevelFilter(logging.WARNING))
        root.addHandler(app_handler)
        (log_dir / app_log_name).chmod(0o600)

    audit_logger = logging.getLogger("streamserver.audit")
    audit_logger.setLevel(logging.INFO)
    audit_logger.propagate = False
    for handler in list(audit_logger.handlers):
        audit_logger.removeHandler(handler)
    if to_file:
        audit_handler = AuditFileHandler(log_dir / audit_log_name)
        audit_logger.addHandler(audit_handler)
    audit_console = logging.StreamHandler(sys.stderr)
    audit_console.setFormatter(logging.Formatter("AUDIT %(asctime)s %(message)s", datefmt=_DATEFMT))
    audit_console.addFilter(redactor)
    if console:
        audit_logger.addHandler(audit_console)

    for name, quiet_level in _QUIET.items():
        logging.getLogger(name).setLevel(quiet_level)

    logging.captureWarnings(True)
    py_warnings = logging.getLogger("py.warnings")
    py_warnings.addFilter(redactor)

    return {
        "app": logging.getLogger("streamserver"),
        "audit": audit_logger,
        "media": logging.getLogger("streamserver.media"),
        "api": logging.getLogger("streamserver.api"),
    }


class RequestSummary:
    """Per-request byte/request counters, logged once per response.

    Avoids one log line per Range chunk (which for a feature film is tens of
    thousands) while still leaving a durable record of who streamed what.
    """

    __slots__ = ("started", "requests", "bytes_out", "path", "client")

    def __init__(self, path: str, client: str | None) -> None:
        self.path = path
        self.client = client
        self.started = 0.0
        self.requests = 0
        self.bytes_out = 0

    def as_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "client": self.client,
            "requests": self.requests,
            "bytes": self.bytes_out,
        }


__all__ = ["setup_logging", "RequestSummary", "scrub_text", "RedactingFormatter"]