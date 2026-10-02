"""HTTP Range streaming.

Serves a media file with correct ``Range`` semantics (RFC 9110 §14) so the
player's seek bar works, without ever letting a client choose a path.

Security posture
----------------
* The client addresses media by **integer id**.  The server resolves id ->
  path and that resolved path is re-checked against the library roots *after*
  resolution, so a symlink swapped in between import and playback cannot widen
  access.  This is why there is no ``?path=`` parameter anywhere.
* A short-lived, single-purpose **playback token** is required in addition to
  the session access token, so a leaked access token does not by itself grant
  the right to stream anything.
* ``X-Content-Type-Options: nosniff`` and a fixed ``Content-Type`` mean a
  mislabelled file cannot be coaxed into executing as HTML.

Range handling
--------------
Supports ``bytes=a-b``, ``bytes=a-``, ``bytes=-n``.  Rejects overlapping or
unsatisfiable ranges with 416 and a ``Content-Range: bytes */size`` header,
which is what players expect, rather than silently returning the whole file.
"""

from __future__ import annotations

import logging
import mimetypes
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator

from ..security.paths import MediaRootRegistry, PathViolation

log = logging.getLogger(__name__)

RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$", re.I)

#: Containers we can stream.  Anything else is offered as a download.
CONTAINER_CONTENT_TYPES = {
    ".mkv": "video/x-matroska",
    ".mp4": "video/mp4",
    ".m4v": "video/x-m4v",
    ".mov": "video/quicktime",
    ".avi": "video/x-msvideo",
    ".webm": "video/webm",
    ".ts": "video/mp2t",
    ".m2ts": "video/mp2t",
    ".mpg": "video/mpeg",
    ".mpeg": "video/mpeg",
    ".wmv": "video/x-ms-wmv",
    ".flv": "video/x-flv",
    ".ogv": "video/ogg",
    ".3gp": "video/3gpp",
}

DEFAULT_CONTENT_TYPE = "application/octet-stream"


class RangeNotSatisfiable(Exception):
    def __init__(self, size: int) -> None:
        super().__init__(f"range not satisfiable for a {size} byte file")
        self.size = size


class RangeNotParseable(Exception):
    """A syntactically invalid Range header.  Ignored per RFC, not rejected."""


@dataclass(slots=True)
class ResolvedRange:
    """A concrete byte interval to serve."""

    start: int
    end: int          # inclusive
    total_size: int
    is_partial: bool = True

    @property
    def length(self) -> int:
        return self.end - self.start + 1

    def content_range_header(self) -> str:
        return f"bytes {self.start}-{self.end}/{self.total_size}"

    def unsatisfied_header(self) -> str:
        return f"bytes */{self.total_size}"


def parse_range_header(header: str | None, size: int) -> ResolvedRange | None:
    """Parse a single-range ``Range`` header.

    Returns ``None`` when the header is absent (serve the whole file).
    Raises :class:`RangeNotParseable` for malformed syntax, which callers
    ignore, and :class:`RangeNotSatisfiable` for a well-formed range that
    falls outside the file, which becomes a 416.
    """
    if header is None:
        return None
    value = header.strip()
    if not value:
        return None
    if "," in value:
        # Multi-range would need a multipart/byteranges body; no player we
        # target asks for it, and serving the whole file is a valid response.
        return None

    match = RANGE_RE.match(value)
    if not match:
        raise RangeNotParseable(value)

    raw_start, raw_end = match.group(1), match.group(2)
    if not raw_start and not raw_end:
        raise RangeNotParseable(value)

    if not raw_start:
        # Suffix form: "bytes=-500" means the last 500 bytes.
        suffix_length = int(raw_end)
        if suffix_length == 0:
            raise RangeNotSatisfiable(size)
        if size == 0:
            raise RangeNotSatisfiable(size)
        start = max(0, size - suffix_length)
        return ResolvedRange(start=start, end=size - 1, total_size=size)

    start = int(raw_start)
    if start >= size:
        raise RangeNotSatisfiable(size)
    end = int(raw_end) if raw_end else size - 1
    end = min(end, size - 1)
    if end < start:
        raise RangeNotSatisfiable(size)
    return ResolvedRange(start=start, end=end, total_size=size)


def content_type_for(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in CONTAINER_CONTENT_TYPES:
        return CONTAINER_CONTENT_TYPES[suffix]
    guessed, _ = mimetypes.guess_type(path.name)
    if guessed and guessed.startswith("video/"):
        return guessed
    return DEFAULT_CONTENT_TYPE


def validate_media_file(path: Path) -> int:
    """Confirm the file is a readable regular file and return its size.

    Called immediately before opening, so a file replaced by a FIFO, a device
    or a symlink to ``/dev/zero`` between import and playback is rejected.
    """
    try:
        info = path.stat()
    except OSError as exc:
        raise PathViolation(f"media file is unavailable: {exc}") from exc
    if not stat.S_ISREG(info.st_mode):
        raise PathViolation("media path is not a regular file")
    if info.st_size == 0:
        raise PathViolation("media file is empty")
    return info.st_size


def resolve_stream_target(
    path: Path, registry: MediaRootRegistry
) -> tuple[Path, int]:
    """Re-check containment at request time and return ``(path, size)``.

    The import already validated the path; doing it again here closes the
    window in which the file is moved, replaced or re-pointed by a symlink.
    """
    resolved = registry.resolve(path)
    return resolved, validate_media_file(resolved)


async def iter_file_chunks(
    path: Path,
    start: int,
    length: int,
    *,
    chunk_size: int = 1024 * 1024,
) -> AsyncIterator[bytes]:
    """Yield ``length`` bytes from ``path`` starting at ``start``.

    Reads in bounded chunks through a single open file handle.  The file is
    opened with ``O_NOFOLLOW`` where the platform supports it, so a symlink
    planted at the last moment cannot redirect the read.
    """
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC

    remaining = length
    fd = os.open(str(path), flags)
    try:
        os.lseek(fd, start, os.SEEK_SET)
        while remaining > 0:
            want = min(chunk_size, remaining)
            data = os.read(fd, want)
            if not data:
                # Truncated mid-read; stop cleanly rather than hanging the client.
                log.warning("media file %s ended %d bytes early", path, remaining)
                return
            remaining -= len(data)
            yield data
    finally:
        os.close(fd)


def build_headers(
    *,
    path: Path,
    rng: ResolvedRange,
    is_partial: bool,
    playback_session_id: str | None = None,
    inline: bool = True,
) -> dict[str, str]:
    headers = {
        "Content-Type": content_type_for(path),
        "Accept-Ranges": "bytes",
        # A mislabelled file must never be rendered as HTML by a browser that
        # somehow reaches the endpoint.
        "X-Content-Type-Options": "nosniff",
        "Cache-Control": "private, max-age=3600" if inline else "attachment",
        # Stop a naive downloader from resuming with a stale copy.
        "Content-Disposition": (
            "inline" if inline else f'attachment; filename="{path.name}"'
        ),
    }
    if is_partial:
        headers["Content-Range"] = rng.content_range_header() if rng else ""
        headers["Content-Length"] = str(rng.length)
    else:
        headers["Content-Length"] = str(rng.total_size if rng else 0)
    if playback_session_id:
        headers["X-Playback-Session"] = playback_session_id
    return {k: v for k, v in headers.items() if v != ""}


def clamp_span(start: int, end: int, total: int, max_span: int) -> tuple[int, int]:
    """Bound a range so one request cannot pin a worker for the whole film."""
    end = min(end, total - 1)
    if max_span > 0 and end - start + 1 > max_span:
        end = start + max_span - 1
    return start, end


def human_size(num_bytes: int) -> str:
    value = float(num_bytes)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:.1f} TiB"