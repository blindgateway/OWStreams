"""Media import.

The pipeline the brief asks for::

    file -> scan -> parse name -> probe -> metadata -> media row -> artwork

Everything is idempotent: re-running an import over the same tree updates
existing rows instead of duplicating them, keyed on the absolute path (which is
unique in the schema) with a size/mtime/inode signature so an *edited* file is
re-probed and an untouched one is skipped without ever touching the slow HDD's
data.

Two entry points share this code:

* :meth:`Importer.scan` -- walk the configured roots.
* :meth:`Importer.add_path` -- one specific file (CLI, REST, IPC).
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Iterator

from ..config import Config
from ..db import repositories as repo
from ..db.database import Database
from ..db.repositories import MediaFilter
from ..media.naming import ParsedName, clean_title, parse_filename, sort_key
from ..media.probe import MediaProbe, ProbeResult
from ..media.scanner import ScannedFile, scan_tree
from ..metadata.providers import MediaMetadata, MetadataProvider, ProviderChain
from ..security.audit import AuditLogger, SecurityEventTypes
from ..security.paths import MediaRootRegistry, PathViolation, slugify

log = logging.getLogger(__name__)

ProgressFn = Callable[[str, int, int], None]


class MediaImportError(Exception):
    """Import failed.  Distinct from the builtin so it reads clearly here."""


class PathNotImportable(MediaImportError):
    pass


@dataclass(slots=True)
class ImportStats:
    added: int = 0
    updated: int = 0
    skipped: int = 0
    removed: int = 0
    failed: int = 0
    probed: int = 0
    metadata_hits: int = 0
    duration_seconds: float = 0.0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "added": self.added, "updated": self.updated, "skipped": self.skipped,
            "removed": self.removed, "failed": self.failed, "probed": self.probed,
            "metadata_hits": self.metadata_hits,
            "duration_seconds": round(self.duration_seconds, 2),
            "errors": self.errors[:50],
        }


@dataclass(slots=True)
class ImportResult:
    media_id: int
    slug: str
    title: str
    created: bool
    metadata_source: str


class Importer:
    def __init__(
        self,
        db: Database,
        config: Config,
        registry: MediaRootRegistry,
        *,
        provider: MetadataProvider | ProviderChain | None = None,
        probe: MediaProbe | None = None,
        audit: AuditLogger | None = None,
    ) -> None:
        self.db = db
        self.config = config
        self.registry = registry
        self.probe = probe or MediaProbe(
            config.media.ffprobe_path, config.media.ffprobe_timeout_seconds
        )
        self.provider = provider or ProviderChain([])
        self.audit = audit or AuditLogger(db, enabled=False)

    # ------------------------------------------------------------------
    # Public entry points
    # ------------------------------------------------------------------
    def add_path(
        self,
        path: Path | str,
        *,
        title: str | None = None,
        year: int | None = None,
        metadata: MediaMetadata | None = None,
        refresh_metadata: bool = False,
        content_type: str | None = None,
        external_ids: dict[str, str] | None = None,
    ) -> ImportResult:
        """Import one file.  Safe to call repeatedly with the same path."""
        target = self._resolve_media_path(path)
        parsed = parse_filename(target.name)

        if title:
            parsed.title = clean_title(title)
        if year:
            parsed.year = year

        existing = repo.get_media_by_path(self.db, str(target))
        result = self._import_one(
            target, parsed, existing=existing,
            metadata=metadata, refresh_metadata=refresh_metadata,
            content_type=content_type, external_ids=external_ids,
        )
        self.db.optimize()
        return result

    def scan(
        self,
        roots: Iterable[Path | str] | None = None,
        *,
        remove_missing: bool = False,
        reimport_all: bool = False,
        progress: ProgressFn | None = None,
        limit: int | None = None,
    ) -> ImportStats:
        """Walk the library roots and import everything new or changed."""
        started = time.monotonic()
        stats = ImportStats()
        target_roots = [Path(r) for r in (roots or self.registry.list_roots())]

        processed = 0
        for root in target_roots:
            try:
                self.registry.resolve(root)
            except PathViolation as exc:
                stats.errors.append(f"{root}: {exc}")
                continue

            if progress:
                progress(f"scanning {root}", 0, 0)

            for record in scan_tree(root):
                if limit is not None and processed >= limit:
                    break
                processed += 1
                if progress:
                    progress(f"importing {record.name}", processed, 0)
                try:
                    self._import_scanned(record, stats, reimport_all=reimport_all)
                except PathViolation as exc:
                    stats.skipped += 1
                    log.info("skipping %s: %s", record.path, exc)
                except Exception as exc:  # noqa: BLE001 - one bad file must not stop a scan
                    stats.failed += 1
                    stats.errors.append(f"{record.name}: {exc}")
                    log.exception("failed to import %s", record.path)

        if remove_missing:
            stats.removed = self._remove_missing(target_roots)

        stats.duration_seconds = time.monotonic() - started
        self.db.optimize()
        self.audit.record(
            SecurityEventTypes.IMPORT_RUN, outcome="ok",
            detail=stats.as_dict() | {"roots": len(target_roots)},
        )
        return stats

    def verify_integrity(self, *, deep: bool = False) -> dict[str, int]:
        """Re-check every media row's file on disk.

        Cheap by default (``stat`` only).  ``deep=True`` re-probes, which
        reads from the slow HDD and is therefore an explicit choice.
        """
        stats = {"ok": 0, "missing": 0, "repaired": 0, "failed": 0,
                 "checked": 0, "skipped": 0}
        if self.registry.is_empty:
            # With no roots configured, every path would fail containment and
            # the whole library would be marked missing.  That is a
            # configuration problem, not 1000 broken files.
            log.warning(
                "verify_integrity: no library roots are configured, so no file "
                "can be checked; nothing was modified"
            )
            stats["skipped"] = len(repo.media_needing_integrity_check(self.db))
            return stats
        for row in repo.media_needing_integrity_check(self.db):
            stats["checked"] += 1
            media_id = int(row["id"])
            try:
                path, size = resolve_and_stat(Path(row["media_path"]), self.registry)
            except PathViolation:
                repo.set_integrity_state(self.db, media_id, "missing")
                stats["missing"] += 1
                continue

            if row["integrity_state"] != "ok":
                repo.update_media(self.db, media_id, file_size_bytes=size,
                                  file_modified_at=_iso_mtime(path))
                repo.set_integrity_state(self.db, media_id, "ok")
                stats["repaired"] += 1
            else:
                stats["ok"] += 1

            if deep:
                probe_result = self.probe.probe(path)
                if probe_result.ok:
                    repo.update_media(self.db, media_id, **probe_result.as_media_fields())
                    repo.set_media_streams(self.db, media_id, probe_result.as_stream_rows())
                    repo.set_integrity_state(self.db, media_id, "ok")
                else:
                    repo.set_integrity_state(self.db, media_id, "corrupt")
        return stats

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _resolve_media_path(self, path: Path | str) -> Path:
        """Resolve and contain a media path; reject anything outside the roots."""
        try:
            resolved = self.registry.resolve(Path(path))
        except PathViolation as exc:
            raise PathNotImportable(str(exc)) from exc
        if not resolved.exists():
            raise PathNotImportable(f"file does not exist: {resolved}")
        if not resolved.is_file():
            raise PathNotImportable(f"not a regular file: {resolved}")
        return resolved

    def _import_scanned(self, record: ScannedFile, stats: ImportStats, *,
                        reimport_all: bool) -> None:
        existing = repo.get_media_by_path(self.db, str(record.path))
        if existing and not reimport_all:
            same_size = int(existing["file_size_bytes"] or 0) == record.size
            same_inode = (not existing["file_inode"]) or existing["file_inode"] == record.inode
            same_mtime = _stored_mtime(existing["file_modified_at"]) == int(record.modified_at)
            if same_size and same_inode and same_mtime \
                    and existing["integrity_state"] == "ok":
                stats.skipped += 1
                return

        parsed = parse_filename(record.name)
        # A directory named like the film titles its files ("Dune (2021)/").
        parent_hint = parse_filename(record.path.parent.name)
        if parent_hint.title and parent_hint.title not in {"", "Untitled"}:
            if parsed.title == "Untitled" or len(parsed.title) < 3:
                parsed.title = parent_hint.title
            if parsed.year is None and parent_hint.year:
                parsed.year = parent_hint.year

        self._import_one(
            record.path, parsed, existing=existing,
            metadata=None, refresh_metadata=bool(existing),
            sidecars=record.sidecars, size=record.size, mtime=record.modified_at,
            inode=record.inode, stats=stats,
        )

    def _import_one(
        self,
        path: Path,
        parsed: ParsedName,
        *,
        existing: dict | None,
        metadata: MediaMetadata | None,
        refresh_metadata: bool,
        content_type: str | None = None,
        external_ids: dict[str, str] | None = None,
        sidecars: list[Path] | None = None,
        size: int | None = None,
        mtime: float | None = None,
        inode: str | None = None,
        stats: ImportStats | None = None,
    ) -> ImportResult:
        probe_result = self.probe.probe(path)
        if stats:
            stats.probed += 1

        if not probe_result.ok:
            # An unreadable container still gets a row: the operator can see
            # which file is broken and fix or delete it, instead of it silently
            # vanishing from the library.
            log.warning("%s: %s", path.name, probe_result.error)
            probe_fields: dict = {"file_size_bytes": probe_result.file_size_bytes}
        else:
            probe_fields = probe_result.as_media_fields()

        title = clean_title(parsed.title)
        display_year = parsed.year
        genres: list[str] = []
        tags: list[str] = []
        credits: list[dict] = []
        rating = rating_source = rating_votes = parental = None
        original_title = overview = tagline = release_date = None
        provider_name = "none"

        if metadata is None and (sidecars or refresh_metadata or not existing):
            metadata = self.provider.lookup(parsed, sidecars=sidecars)
        if metadata is not None:
            provider_name = metadata.provider
            if stats:
                stats.metadata_hits += 1
            title = clean_title(metadata.title or title)
            original_title = metadata.original_title
            overview = metadata.overview
            tagline = metadata.tagline
            display_year = metadata.year or display_year
            release_date = metadata.release_date
            genres = list(metadata.genres)
            tags = list(metadata.tags)
            rating = metadata.rating_value
            rating_source = metadata.rating_source
            rating_votes = metadata.rating_vote_count
            parental = metadata.parental_rating
            credits = [
                {
                    **_credit_to_dict(c),
                    "slug": slugify(c.name, max_length=60, fallback="person"),
                }
                for c in (metadata.cast + metadata.directors + metadata.writers)
                if c.name
            ]
            ids = dict(metadata.external_ids)
        else:
            ids = dict(external_ids or {})

        if content_type:
            resolved_type = content_type
        elif parsed.content_type == "episode":
            resolved_type = "episode"
        elif existing:
            resolved_type = existing["content_type"]
        else:
            resolved_type = "movie"

        if external_ids:
            ids.update(external_ids)

        stat_size = size if size is not None else probe_result.file_size_bytes
        stat_mtime = mtime if mtime is not None else (path.stat().st_mtime if path.exists() else 0.0)

        base_slug = slugify(f"{title}-{display_year}" if display_year else title)
        slug = base_slug if not existing else existing["slug"]
        if existing is None:
            slug = repo.unique_slug(self.db, base_slug)

        values: dict = {
            "slug": slug,
            "title": title,
            "original_title": original_title,
            "sort_title": sort_key(title),
            "overview": overview or "",
            "tagline": tagline,
            "year": display_year,
            "release_date": release_date,
            "runtime_minutes": probe_fields.get("runtime_minutes"),
            "content_type": resolved_type,
            "parental_rating": parental,
            "rating_value": rating,
            "rating_source": rating_source,
            "rating_vote_count": rating_votes,
            "media_path": str(path),
            "media_root": str(self.registry.root_for(path) or ""),
            "file_size_bytes": stat_size,
            "file_modified_at": _iso_mtime(path) if mtime is None else _ts_to_iso(mtime),
            "file_inode": inode,
            "container": probe_fields.get("container"),
            "video_codec": probe_fields.get("video_codec"),
            "video_profile": probe_fields.get("video_profile"),
            "width": probe_fields.get("width"),
            "height": probe_fields.get("height"),
            "aspect_ratio": probe_fields.get("aspect_ratio"),
            "frame_rate": probe_fields.get("frame_rate"),
            "bitrate_bps": probe_fields.get("bitrate_bps"),
            "hdr_format": probe_fields.get("hdr_format"),
            "color_space": probe_fields.get("color_space"),
            "audio_codec": probe_fields.get("audio_codec"),
            "audio_channels": probe_fields.get("audio_channels"),
            "audio_languages": probe_fields.get("audio_languages"),
            "subtitle_languages": probe_fields.get("subtitle_languages"),
            "external_ids": ids,
            "source": "import",
            "searchable_text": _searchable(title, overview, genres, credits),
        }

        if existing:
            # Preserve operator edits to descriptive fields when the new pass
            # has nothing better to say.
            for field_name in ("overview", "year", "runtime_minutes", "rating_value",
                               "poster_key", "backdrop_key", "parental_rating"):
                if values.get(field_name) in (None, "") and existing.get(field_name) not in (None, ""):
                    values[field_name] = existing[field_name]
            media_id = repo.update_media(self.db, int(existing["id"]), **values)
            media_id = int(existing["id"])
            created = False
            if stats:
                stats.updated += 1
        else:
            media_id = repo.create_media(self.db, values)
            created = True
            if stats:
                stats.added += 1

        if genres:
            repo.set_media_genres(self.db, media_id, [
                (g, slugify(g, max_length=48, fallback="genre")) for g in genres
            ])
        if tags:
            repo.set_media_tags(self.db, media_id, [
                (t, slugify(t, max_length=48, fallback="tag")) for t in tags
            ])
        if credits:
            repo.set_media_credits(self.db, media_id, credits)
        if probe_result.ok:
            repo.set_media_streams(self.db, media_id, probe_result.as_stream_rows())
        repo.set_external_ids(self.db, media_id, ids)
        repo.set_integrity_state(self.db, media_id, "ok" if probe_result.ok else "corrupt")
        repo.rebuild_search_index(self.db, media_id)

        return ImportResult(
            media_id=media_id, slug=slug, title=title,
            created=created, metadata_source=provider_name,
        )

    def _remove_missing(self, roots: Iterable[Path]) -> int:
        """Drop rows whose files have vanished under the given roots."""
        removed = 0
        if self.registry.is_empty:
            log.warning("remove_missing: no library roots configured; nothing removed")
            return 0
        if self.registry.is_empty:
            log.warning("remove_missing: no library roots configured; nothing removed")
            return 0
        root_strings = [str(self.registry.resolve(r)) for r in roots
                        if _safe_resolve(self.registry, r)]
        for row in repo.all_media_paths(self.db).items():
            path_str, media_id = row
            if not any(path_str.startswith(root + os.sep) for root in root_strings):
                continue
            if not os.path.exists(path_str):
                log.info("removing %s: file is gone", path_str)
                repo.delete_media(self.db, media_id)
                removed += 1
        return removed

    # ------------------------------------------------------------------
    # Manual metadata editing (admin API)
    # ------------------------------------------------------------------
    def update_metadata(self, media_id: int, patch: dict) -> dict | None:
        """Apply an operator-supplied metadata patch to one media row."""
        allowed = {
            "title", "original_title", "overview", "tagline", "year",
            "release_date", "runtime_minutes", "parental_rating",
            "rating_value", "rating_source", "content_type", "poster_key",
            "backdrop_key", "logo_key", "hidden",
        }
        updates = {k: v for k, v in patch.items() if k in allowed}
        if "title" in updates:
            updates["sort_title"] = sort_key(updates["title"])
            existing = repo.get_media(self.db, media_id)
            if existing:
                updates["slug"] = repo.unique_slug(
                    self.db, slugify(updates["title"]), exclude_id=media_id
                )
        updated = repo.update_media(self.db, media_id, **updates)
        repo.rebuild_search_index(self.db, media_id)
        return updated

    def set_artwork(self, media_id: int, *, poster_key: str | None = None,
                    backdrop_key: str | None = None,
                    logo_key: str | None = None) -> dict | None:
        return repo.update_media(
            self.db, media_id,
            poster_key=poster_key, backdrop_key=backdrop_key, logo_key=logo_key,
        )

    def create_collection(self, name: str, description: str = "",
                          media_ids: list[int] | None = None) -> int:
        slug = slugify(name, max_length=64, fallback="collection")
        existing = self.db.query_one("SELECT id FROM collections WHERE slug = ?", (slug,))
        collection_id = int(existing["id"]) if existing else repo.create_collection(
            self.db, name, slug, description
        )
        for index, media_id in enumerate(media_ids or []):
            repo.add_to_collection(self.db, collection_id, int(media_id), index)
        return collection_id

    def reset_search_index(self) -> int:
        return repo.rebuild_all_search_index(self.db)

    def stats(self) -> dict:
        return repo.library_stats(self.db)


def _stored_mtime(iso_value: str | None) -> int:
    """Whole-second epoch from a stored ISO timestamp (0 when unknown)."""
    if not iso_value:
        return 0
    from datetime import UTC, datetime

    try:
        parsed = datetime.fromisoformat(str(iso_value))
    except (TypeError, ValueError):
        return 0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return int(parsed.timestamp())


def _safe_resolve(registry: MediaRootRegistry, path: Path) -> bool:
    try:
        registry.resolve(path)
    except PathViolation:
        return False
    return True


def resolve_and_stat(path: Path, registry: MediaRootRegistry) -> tuple[Path, int]:
    resolved = registry.resolve(path)
    return resolved, resolved.stat().st_size


def _iso_mtime(path: Path) -> str | None:
    try:
        return _ts_to_iso(path.stat().st_mtime)
    except OSError:
        return None


def _ts_to_iso(ts: float) -> str:
    from datetime import UTC, datetime

    return datetime.fromtimestamp(ts, tz=UTC).replace(microsecond=0).isoformat()


def _credit_to_dict(credit) -> dict:
    """Flatten a provider ``Credit`` into the mapping the repository wants."""
    import dataclasses

    if isinstance(credit, dict):
        return dict(credit)
    return dataclasses.asdict(credit)


def _credit_to_dict(credit) -> dict:
    """Flatten a provider ``Credit`` into the mapping the repository wants."""
    import dataclasses

    if isinstance(credit, dict):
        return dict(credit)
    return dataclasses.asdict(credit)


def _searchable(title: str, overview: str | None, genres: list[str],
                credits: list[dict]) -> str:
    parts = [title, overview or "", " ".join(genres)]
    parts.extend(c.get("name", "") for c in credits[:30])
    return " ".join(p for p in parts if p)[:8000]


def iter_existing_media(db: Database) -> Iterator[dict]:
    for row in db.query("SELECT * FROM media ORDER BY id"):
        yield dict(row)


def dump_library(db: Database) -> str:
    """Export the library as JSON -- used for migration and for diffing."""
    media = list(iter_existing_media(db))
    for item in media:
        item["genres"] = [g["slug"] for g in repo.get_media_genre_slugs(db, int(item["id"]))]
    return json.dumps({"version": 1, "media": media}, indent=2, ensure_ascii=False)


def unused_filter_reference() -> MediaFilter:  # pragma: no cover
    """Kept so static analysers see the import; the filter is used by routes."""
    return MediaFilter()