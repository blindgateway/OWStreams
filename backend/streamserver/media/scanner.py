"""Filesystem scanning.

Walks configured library roots and yields media files, with two properties
that matter on a real box:

* **It never follows symlinks out of the library.**  A symlinked directory is
  skipped by default, so a stray ``ln -s / home`` inside the media folder
  cannot turn a scan into a full-disk crawl.
* **It never reads file contents.**  Discovery is a directory walk plus a
  ``stat``; probing happens later and only for files that are about to be
  imported or re-verified.  Walking a 1000-title library on the slow HDD is
  therefore cheap.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from .naming import VIDEO_SUFFIXES

log = logging.getLogger(__name__)

#: Extensions treated as playable video.
VIDEO_EXTENSIONS = frozenset(VIDEO_SUFFIXES)
#: Metadata sidecars we read but never import as media.
SIDECAR_EXTENSIONS = (".nfo", ".json", ".yaml", ".yml", ".jpg", ".jpeg", ".png", ".webp")

#: Directories that are never media.  Skipping these by name avoids walking
#: into sample/, @eaDir/ and friends, which is where a 1 TB disk hides its worst
#: inode count.
SKIP_DIRECTORIES = frozenset({
    "@eaDir", "#recycle", ".recycle", "$RECYCLE.BIN", "system volume information",
    "lost+found", ".git", ".svn", "extras", "extras/Featurettes", "extras/Behind the Scenes",
    "extras/Deleted Scenes", "extras/Interviews", "extras/Featurettes/Bloopers",
    "bonus", "bonusfeatures", "proof", "proofs", "sample", "samples", "subs",
    "trailers", "screenshots", "artwork", "artworks", "covers",
})

#: Files larger than this are almost certainly not a single title.
MAX_REASONABLE_FILE_BYTES = 200 * 1024 * 1024 * 1024


@dataclass(slots=True)
class ScannedFile:
    path: Path
    size: int
    modified_at: float
    inode: str | None
    depth: int
    sidecars: list[Path]

    @property
    def name(self) -> str:
        return self.path.name


def is_media_file(path: Path) -> bool:
    return path.suffix.lower() in VIDEO_EXTENSIONS


def _should_skip_dir(name: str) -> bool:
    lowered = name.lower()
    if lowered in SKIP_DIRECTORIES:
        return True
    # "Extras", "Sample", "proof" in any casing, with or without separators.
    stripped = lowered.replace("_", "").replace("-", "").replace(" ", "")
    return stripped in {
        "extras", "extrasfeaturettes", "extrablu-rays", "extrasbehindusthe scenes",
        "extrasscenescuts", "featurettes", "behindthescenes", "interviews",
        "deletedscenes", "bonus", "bonusfeatures", "sample", "samples",
        "proof", "proofs", "trailers", "screenshots", "subs", "subtitles",
    }


def iter_media_files(
    root: Path,
    *,
    extensions: frozenset[str] | set[str] = VIDEO_EXTENSIONS,
    follow_symlinks: bool = False,
    max_depth: int = 8,
) -> Iterator[Path]:
    """Yield media files under ``root``.

    Implemented iteratively rather than with :func:`os.walk` so that symlinked
    directories can be skipped reliably (os.walk's ``followlinks`` flag is
    all-or-nothing) and so a permission error on one directory does not abort
    the whole scan.
    """
    root = Path(root)
    if not root.is_dir():
        return

    stack: list[tuple[Path, int]] = [(root, 0)]
    while stack:
        current, depth = stack.pop()
        try:
            entries = list(os.scandir(current))
        except (PermissionError, OSError) as exc:
            log.warning("cannot read directory %s: %s", current, exc)
            continue

        for entry in entries:
            try:
                is_dir = entry.is_dir(follow_symlinks=follow_symlinks)
                is_link = entry.is_symlink()
            except OSError:
                continue
            if is_link and not follow_symlinks:
                # A symlinked *file* is still worth scanning; a symlinked
                # directory is not, because it can point anywhere.
                if entry.is_dir():
                    continue
            if is_dir:
                if depth >= max_depth or _should_skip_dir(entry.name):
                    continue
                stack.append((Path(entry.path), depth + 1))
                continue
            if extensions and Path(entry.name).suffix.lower() not in extensions:
                continue
            yield Path(entry.path)


def find_sidecars(media_path: Path) -> list[Path]:
    """Metadata files sitting next to ``media_path``.

    Two conventions are supported: ``<name>.nfo`` and ``<name>/*.json`` -- the
    latter being how Plex and Emby organise per-title extras.
    """
    sidecars: list[Path] = []
    try:
        for suffix in SIDECAR_EXTENSIONS:
            candidate = media_path.with_suffix(suffix)
            if candidate.exists() and candidate.is_file():
                sidecars.append(candidate)
        artwork_dir = media_path.parent / media_path.stem
        if artwork_dir.is_dir():
            for suffix in (".nfo", ".json"):
                candidate = artwork_dir / f"{suffix.lstrip('.')}"
                for probe in (
                    artwork_dir / f"movie{suffix}",
                    artwork_dir / f"info{suffix}",
                    artwork_dir / f"{suffix}",
                ):
                    if probe.exists() and probe.is_file() and probe not in sidecars:
                        sidecars.append(probe)
                        break
    except OSError as exc:
        log.debug("cannot list sidecars for %s: %s", media_path, exc)
    return sidecars


def scan_tree(
    root: Path,
    *,
    extensions: frozenset[str] | set[str] = VIDEO_EXTENSIONS,
    follow_symlinks: bool = False,
    with_sidecars: bool = True,
    max_depth: int = 8,
    progress: "ProgressCallback | None" = None,
) -> Iterator[ScannedFile]:
    """Walk ``root`` yielding :class:`ScannedFile` records, sorted by path.

    Sorting makes imports deterministic, which makes re-runs reproducible and
    makes test failures reproducible.
    """
    found: list[Path] = list(
        iter_media_files(root, extensions=extensions, follow_symlinks=follow_symlinks,
                         max_depth=max_depth)
    )
    for path in sorted(found):
        try:
            stat = path.stat()
        except OSError as exc:
            log.warning("cannot stat %s: %s", path, exc)
            continue
        if stat.st_size <= 0:
            log.debug("skipping empty file %s", path)
            continue
        if stat.st_size > MAX_REASONABLE_FILE_BYTES:
            log.warning("skipping implausibly large file %s (%d bytes)", path, stat.st_size)
            continue
        record = ScannedFile(
            path=path,
            size=stat.st_size,
            modified_at=stat.st_mtime,
            inode=_inode_of(stat),
            depth=len(path.relative_to(root).parts) - 1,
            sidecars=find_sidecars(path) if with_sidecars else [],
        )
        if progress:
            progress(record)
        yield record


def _inode_of(stat: os.stat_result) -> str | None:
    inode = getattr(stat, "st_ino", None)
    return str(inode) if inode else None


ProgressCallback = "callable"


def file_signature(path: Path) -> tuple[int, float, str | None]:
    """(size, mtime, inode) -- enough to detect a replaced or edited file."""
    try:
        stat = path.stat()
    except OSError:
        return (0, 0.0, None)
    return (stat.st_size, stat.st_mtime, _inode_of(stat))


def find_by_path(target: Path) -> Path | None:
    """Resolve a path that may sit under a symlinked root."""
    try:
        return target.resolve(strict=False)
    except (OSError, RuntimeError):
        return None