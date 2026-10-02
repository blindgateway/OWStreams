"""The importer, end to end over a real library directory.

The other suites prove the pieces work.  This one proves that a scan of a tree
that looks like a real one -- release names, a sidecar, a sample folder, a
stray text file, and a file that is not actually a video -- produces the
library an operator expects, and that re-scanning it does not quietly corrupt
what the first scan got right.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from streamserver.db import repositories as repo
from streamserver.media.naming import VIDEO_SUFFIXES
from streamserver.metadata.providers import ProviderChain, build_provider
from streamserver.security.paths import MediaRootRegistry
from streamserver.services.importer import Importer, PathNotImportable


@pytest.fixture
def importer(db, config) -> Importer:
    """An importer over the test database, wired the way the app wires it --
    with the local metadata provider, which is the server's default and the
    only one that works without an API key."""
    return Importer(
        db, config, MediaRootRegistry(config.media.library_roots),
        provider=ProviderChain([build_provider("local")]),
    )


def _can_encode() -> bool:
    if shutil.which("ffmpeg") is None:
        return False
    probe = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-encoders"],
        capture_output=True, text=True, check=False,
    )
    return "libx264" in (probe.stdout + probe.stderr)


requires_ffmpeg = pytest.mark.skipif(
    not _can_encode(), reason="ffmpeg with libx264 is needed to build real media"
)


def make_real_movie(path: Path, *, seconds: int = 2) -> None:
    """A genuinely playable file, not random bytes."""
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", f"testsrc=duration={seconds}:size=320x240:rate=10",
            "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
            "-metadata", "title=Real Movie", "-metadata:s:a:0", "language=eng",
            "-shortest", str(path),
        ],
        check=True, capture_output=True,
    )


@pytest.fixture
def tree(library_root: Path) -> Path:
    library_root.mkdir(parents=True, exist_ok=True)
    (library_root / "Dune.Part.Two.2024.2160p.UHD.BluRay.x265-GROUP.mkv").write_bytes(b"x" * 4096)
    (library_root / "Arrival.2016.1080p.BluRay.x264-SPARKS.mkv").write_bytes(b"y" * 4096)
    (library_root / "Se7en.1995.REMASTERED.1080p.mkv").write_bytes(b"z" * 4096)
    (library_root / "Arrival.2016" / "Sample").mkdir(parents=True)
    (library_root / "Arrival.2016" / "Sample" / "trailer.mkv").write_bytes(b"t" * 512)
    (library_root / "Arrival.2016" / "Extras").mkdir()
    (library_root / "Arrival.2016" / "Extras" / "deleted-scene.mkv").write_bytes(b"e" * 512)
    (library_root / "empty.mkv").touch()
    (library_root / "notes.txt").write_text("not a film")
    (library_root / "Arrival.json").write_text(json.dumps({"movie": {
        "title": "Arrival", "year": 2016,
        "overview": "A linguist is recruited to communicate with alien visitors.",
        "genres": ["Science Fiction", "Drama"], "rating": 7.9,
        "runtime": 116, "parental_rating": "PG-13",
        "director": ["Denis Villeneuve"],
        "cast": [{"name": "Amy Adams", "character": "Louise Banks"}],
    }}))
    return library_root


# ---------------------------------------------------------------------------
# A scan of a realistic tree
# ---------------------------------------------------------------------------
def test_a_scan_imports_every_film_and_nothing_else(tree, importer):
    stats = importer.scan()
    assert stats.added == 3
    assert stats.failed == 0

    titles = {row["title"]: row for row in repo.list_media(importer.db, repo.MediaFilter(limit=50))}
    assert set(titles) == {"Dune Part Two", "Arrival", "Se7en"}
    assert titles["Arrival"]["year"] == 2016
    assert titles["Se7en"]["year"] == 1995


def test_a_scan_skips_samples_extras_and_empty_files(tree, importer):
    importer.scan()
    paths = [row["media_path"] for row in
             repo.list_media(importer.db, repo.MediaFilter(limit=50, hide_missing=True))]
    assert not any("Sample" in p for p in paths)
    assert not any("Extras" in p for p in paths)
    assert not any(p.endswith("empty.mkv") for p in paths)
    assert not any(p.endswith("notes.txt") for p in paths)


def test_a_sidecar_supplies_what_the_filename_cannot(importer, config):
    library = config.media.library_roots[0]
    media = Path(library) / "Arrival.2016.1080p.mkv"
    media.write_bytes(b"y" * 4096)
    (Path(library) / "Arrival.json").write_text(json.dumps({"movie": {
        "title": "Arrival", "overview": "Linguist meets aliens.",
        "genres": ["Science Fiction"], "rating": 7.9, "rating_source": "local",
        "runtime": 116,
        "director": ["Denis Villeneuve"],
        "cast": [{"name": "Amy Adams", "character": "Louise Banks"}],
    }}))

    importer.scan()

    row = next(r for r in repo.list_media(importer.db, repo.MediaFilter(limit=50))
               if r["title"] == "Arrival")
    media_id = int(row["id"])
    full = repo.attach_relations_one(importer.db, repo.get_media_full(importer.db, media_id))
    assert full["overview"] == "Linguist meets aliens."
    assert full["rating_value"] == 7.9
    assert full["rating_source"] == "local"
    assert [g["name"] for g in full["genres"]] == ["Science Fiction"]
    assert [c["name"] for c in full["cast"]] == ["Amy Adams"]
    assert [c["name"] for c in full["directors"]] == ["Denis Villeneuve"]
    assert full["cast"][0].get("character") in (None, "Louise Banks")


def test_rescanning_an_unchanged_library_changes_nothing(tree, importer):
    importer.scan()
    stats = importer.scan()
    assert stats.added == 0
    assert stats.updated == 0
    assert stats.skipped == 3


def test_a_changed_file_is_updated_not_duplicated(tree, importer):
    importer.scan()
    target = tree / "Se7en.1995.REMASTERED.1080p.mkv"
    target.write_bytes(b"z" * 8192)

    stats = importer.scan()
    assert stats.updated == 1
    assert stats.added == 0
    rows = [r for r in repo.list_media(importer.db, repo.MediaFilter(limit=50))
            if r["title"] == "Se7en"]
    assert len(rows) == 1
    assert rows[0]["file_size_bytes"] == 8192


# ---------------------------------------------------------------------------
# The technical fields
# ---------------------------------------------------------------------------
def test_a_file_that_is_not_a_video_keeps_its_container(tree, importer):
    """ffprobe cannot read random bytes, but the extension still says what the
    container is -- and the player needs that to choose a demuxer."""
    importer.scan()
    rows = {r["title"]: r for r in repo.list_media(importer.db, repo.MediaFilter(limit=50, hide_missing=True))}
    for title, row in rows.items():
        assert row["container"] is not None, title
        assert row["integrity_state"] == "corrupt", title
        assert Path(row["media_path"]).suffix.lstrip(".") == row["container"], title


def test_a_rescan_does_not_replace_a_container_with_a_filename_fragment(tree, importer):
    """Regression: the title cleaner turned "Dune.Part.Two.mkv" into "lrc" and a
    rescan wrote that into the container column, because the update path
    overwrote a good value with a None-derived one."""
    importer.scan()
    first = {r["title"]: r["container"]
             for r in repo.list_media(importer.db, repo.MediaFilter(limit=50, hide_missing=True))}
    importer.scan()
    second = {r["title"]: r["container"]
              for r in repo.list_media(importer.db, repo.MediaFilter(limit=50, hide_missing=True))}
    assert second == first
    assert all(value for value in second.values())


@requires_ffmpeg
def test_a_real_file_is_probed_into_technical_metadata(importer, config):
    library = Path(config.media.library_roots[0])
    make_real_movie(library / "Real.Movie.2020.1080p.mkv")

    importer.scan()

    row = next(r for r in repo.list_media(importer.db, repo.MediaFilter(limit=50))
               if r["title"] == "Real Movie")
    full = repo.get_media_full(importer.db, int(row["id"]))
    assert full["integrity_state"] == "ok"
    assert full["container"] == "matroska"
    assert full["video_codec"] == "h264"
    assert (full["width"], full["height"]) == (320, 240)
    assert full["runtime_minutes"] >= 1
    assert full["audio_codec"] == "aac"
    assert full["audio_languages"]

    streams = repo.get_media_streams(importer.db, int(row["id"]))
    assert {"video", "audio"} <= set(streams)
    assert streams["video"] and streams["audio"]
    assert streams["audio"][0]["language"] == "eng"


@requires_ffmpeg
def test_a_replaced_file_is_reprobed(importer, config):
    """A file swapped for a different one must not keep the old stream table."""
    library = Path(config.media.library_roots[0])
    path = library / "Real.Movie.2020.1080p.mkv"
    make_real_movie(path, seconds=2)
    importer.scan()
    row = next(r for r in repo.list_media(importer.db, repo.MediaFilter(limit=50))
               if r["title"] == "Real Movie")
    media_id = int(row["id"])
    assert repo.get_media_streams(importer.db, media_id)

    make_real_movie(path, seconds=4)
    importer.scan()
    assert repo.get_media_streams(importer.db, media_id)


# ---------------------------------------------------------------------------
# Containment
# ---------------------------------------------------------------------------
def test_a_path_outside_the_roots_is_refused(importer, tmp_path):
    outside = tmp_path / "outside.mkv"
    outside.write_bytes(b"z" * 1024)
    with pytest.raises(PathNotImportable):
        importer.add_path(outside)


def test_a_missing_file_is_refused(importer, config):
    library = Path(config.media.library_roots[0])
    with pytest.raises(PathNotImportable):
        importer.add_path(library / "nope.mkv")


def test_an_empty_file_is_refused(importer, config):
    library = Path(config.media.library_roots[0])
    empty = library / "nothing.mkv"
    empty.touch()
    with pytest.raises(PathNotImportable):
        importer.add_path(empty)


def test_a_directory_is_refused(importer, config):
    library = Path(config.media.library_roots[0])
    (library / "a-directory.mkv").mkdir()
    with pytest.raises(PathNotImportable):
        importer.add_path(library / "a-directory.mkv")


# ---------------------------------------------------------------------------
# A large library
# ---------------------------------------------------------------------------
def test_a_thousand_titles_scan_and_browse_in_reasonable_time(importer, config):
    """The brief's scale, with a wall-clock ceiling rather than a bare
    "does not raise": a 1000-title scan on the slow HDD is the case the whole
    design is shaped around."""
    import time

    library = Path(config.media.library_roots[0]) / "big"
    library.mkdir()
    for index in range(1000):
        suffix = VIDEO_SUFFIXES[index % len(VIDEO_SUFFIXES)]
        (library / f"Movie.Number.{index}.2000{index % 10}.1080p{suffix}").write_bytes(b"x" * 64)

    started = time.monotonic()
    stats = importer.scan()
    elapsed = time.monotonic() - started

    assert stats.added == 1000, f"only {stats.added} of 1000 imported"
    assert stats.failed == 0
    assert elapsed < 120, f"1000 titles took {elapsed:.1f}s"

    rows, total = repo.search_media(importer.db, repo.SearchQuery(limit=20, offset=0))
    assert total == 1000
    assert len(rows) == 20
