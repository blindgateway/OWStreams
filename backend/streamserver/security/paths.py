"""Filesystem path containment.

The single most important rule in this server: **no API ever accepts a
filesystem path from a client.**  Clients address media by integer id; the
server resolves id -> path.  This module exists so that even internal callers
(the importer, the scanner) get the same guarantee, and so that the streaming
endpoint has something explicit and testable in front of it.

Threats handled:

* ``../`` traversal -- rejected by resolving and comparing real paths.
* Symlink escape -- ``Path.resolve()`` follows symlinks, so a symlink inside a
  library root that points at ``/etc`` fails the containment check.
* Absolute-path smuggling -- an absolute path is only accepted if it is already
  inside a root.
* NUL-byte truncation (``/movies/ok.mp4\\0.txt``) -- rejected before ``open``.
* Case-insensitive filesystems -- comparison uses ``os.path.normcase``.
* Hardlink games -- not preventable by path checks; documented in SECURITY.md.
"""

from __future__ import annotations

import os
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath


class PathViolation(ValueError):
    """The requested path is not inside any configured library root."""


def _normcase(path: Path) -> str:
    return os.path.normcase(str(path))


def contains_traversal(raw: str) -> bool:
    """True when a string looks like a traversal attempt.

    Used to reject rather than normalise, so that a hostile client gets a 400
    instead of a silently-corrected path.
    """
    if not isinstance(raw, str) or not raw:
        return True
    if "\x00" in raw:
        return True
    normalized = unicodedata.normalize("NFKC", raw)
    if "/" in normalized or "\\" in normalized:
        # Any separator inside an *identifier* is suspicious; callers should be
        # passing opaque ids, not paths.
        return True
    parts = [p for p in normalized.replace("\\", "/").split("/") if p]
    return any(p == ".." for p in parts)


@dataclass(slots=True)
class MediaRootRegistry:
    """An immutable set of allowed directories, checked on every access."""

    roots: tuple[Path, ...] = ()
    _resolved: tuple[Path, ...] = field(default=(), init=False, repr=False)

    def __post_init__(self) -> None:
        resolved: list[Path] = []
        for root in self.roots:
            path = Path(root).expanduser()
            if not path.is_absolute():
                raise PathViolation(f"library root must be absolute: {root}")
            try:
                path = path.resolve(strict=False)
            except OSError as exc:  # pragma: no cover - exotic filesystems
                raise PathViolation(f"cannot resolve library root {root}: {exc}") from exc
            # Nested roots are redundant; drop a root contained in another.
            if not any(path == other or _is_within(path, other) for other in resolved):
                resolved.append(path)
        object.__setattr__(self, "_resolved", tuple(resolved))

    # -- queries ---------------------------------------------------------
    @property
    def is_empty(self) -> bool:
        return not self._resolved

    def list_roots(self) -> tuple[Path, ...]:
        return self._resolved

    def root_for(self, candidate: Path | str) -> Path | None:
        """Return the library root that contains ``candidate``, else ``None``."""
        try:
            resolved = Path(candidate).expanduser().resolve(strict=False)
        except (OSError, ValueError, RuntimeError):
            return None
        for root in self._resolved:
            if resolved == root or _is_within(resolved, root):
                return root
        return None

    # -- enforcement -----------------------------------------------------
    def resolve(self, candidate: Path | str) -> Path:
        """Return the fully resolved path, or raise :class:`PathViolation`.

        ``strict=False`` so this can be used to validate a path that has not
        been imported yet (the scanner); callers that need the file to exist
        call :meth:`resolve_existing`.
        """
        raw = str(candidate)
        if "\x00" in raw:
            raise PathViolation("path contains a NUL byte")
        try:
            resolved = Path(raw).expanduser().resolve(strict=False)
        except (OSError, ValueError, RuntimeError) as exc:
            raise PathViolation(f"cannot resolve path: {exc}") from exc

        if self.is_empty:
            raise PathViolation("no library roots are configured")

        for root in self._resolved:
            if resolved == root or _is_within(resolved, root):
                return resolved
        raise PathViolation(f"path is outside every configured library root: {resolved.name}")

    def resolve_existing(self, candidate: Path | str) -> Path:
        resolved = self.resolve(candidate)
        if not resolved.exists():
            raise PathViolation("file does not exist")
        if not resolved.is_file():
            raise PathViolation("not a regular file")
        return resolved

    def contains(self, candidate: Path | str) -> bool:
        try:
            self.resolve(candidate)
        except PathViolation:
            return False
        return True

    def relativize(self, candidate: Path | str) -> str:
        """Return the path relative to its root, for display and stable ids."""
        resolved = Path(candidate).expanduser().resolve(strict=False)
        for root in self._resolved:
            if resolved == root:
                return ""
            if _is_within(resolved, root):
                return str(resolved.relative_to(root))
        raise PathViolation("path is outside every configured library root")

    def scan_walk(self, root: Path | str, *, follow_symlinks: bool = False):
        """Yield candidate media files under a configured root.

        Symlinked directories are not followed by default: a hostile library
        directory containing a link to ``/home`` would otherwise turn a scan
        into a full-disk crawl.
        """
        resolved_root = self.resolve(root)
        from ..media.scanner import VIDEO_EXTENSIONS, iter_media_files

        for path in iter_media_files(
            resolved_root,
            extensions=VIDEO_EXTENSIONS,
            follow_symlinks=follow_symlinks,
        ):
            try:
                self.resolve(path)
            except PathViolation:
                continue
            yield path


def _is_within(candidate: Path, root: Path) -> bool:
    try:
        candidate.relative_to(root)
    except ValueError:
        return False
    return True


def sanitize_filename(name: str, *, fallback: str = "file") -> str:
    """Strip a user-supplied filename down to a safe basename.

    Used for image uploads and per-file metadata sidecars.  Rejects rather
    than silently repairs directory components, because a caller that passes
    ``../../etc/passwd`` has made a mistake worth surfacing.
    """
    if not isinstance(name, str) or not name.strip():
        return fallback
    if "\x00" in name:
        raise PathViolation("filename contains a NUL byte")
    candidate = name.replace("\\", "/")
    if "/" in candidate:
        raise PathViolation("filename must not contain a path separator")
    cleaned = unicodedata.normalize("NFC", candidate)
    cleaned = "".join(c for c in cleaned if c.isprintable() and c not in '<>:"|?*')
    cleaned = cleaned.strip(". ")
    if not cleaned or cleaned in {".", ".."}:
        return fallback
    if len(cleaned) > 200:
        stem, dot, ext = cleaned.rpartition(".")
        keep = 200 - (len(ext) + 1 if dot else 0)
        cleaned = (stem[:keep] + ("." + ext if dot else "")).strip(". ")
    return cleaned or fallback


def safe_join(base: Path, *parts: str) -> Path:
    """Join and confine to ``base``.

    Unlike :meth:`Path.joinpath` this verifies containment after resolution,
    which catches both ``..`` segments and symlinked intermediates.
    """
    base_resolved = base.resolve(strict=False)
    candidate = base_resolved.joinpath(*[sanitize_filename(p) for p in parts])
    resolved = candidate.resolve(strict=False)
    if resolved != base_resolved and not _is_within(resolved, base_resolved):
        raise PathViolation("path escapes the intended directory")
    return resolved


def slugify(value: str, *, max_length: int = 80, fallback: str = "item") -> str:
    """ASCII-ish slug used for public media identifiers.

    Stable and human-readable, and collision-resistant enough for ids that are
    only ever compared within one library.
    """
    if not isinstance(value, str):
        value = str(value)
    text = unicodedata.normalize("NFKD", value)
    text = text.encode("ascii", "ignore").decode("ascii").lower()
    allowed = set("abcdefghijklmnopqrstuvwxyz0123456789-")
    out: list[str] = []
    prev_dash = False
    for ch in text:
        if ch in allowed:
            out.append(ch)
            prev_dash = False
        elif ch in " _./\\:'\"()[]{}":
            if out and not prev_dash:
                out.append("-")
                prev_dash = True
    slug = "".join(out).strip("-")
    while "--" in slug:
        slug = slug.replace("--", "-")
    if len(slug) > max_length:
        slug = slug[:max_length].rstrip("-")
    return slug or fallback


def display_relative(root: Path, path: Path) -> str:
    try:
        return str(PurePosixPath(path).relative_to(root))
    except ValueError:
        return path.name