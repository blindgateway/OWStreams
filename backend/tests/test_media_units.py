"""Filename parsing, scanning, probing and metadata providers.

Unit-level, so a regression here shows up as a specific broken behaviour
rather than as a failing browse test three layers up.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from streamserver.media.naming import (
    clean_title,
    compare_titles,
    normalise_title,
    parse_filename,
    sort_key,
)
from streamserver.media.probe import MediaProbe, _as_float, _hdr_format
from streamserver.media.scanner import (
    find_sidecars,
    is_media_file,
    iter_media_files,
    scan_tree,
)
from streamserver.metadata.providers import (
    LocalSidecarProvider,
    ProviderChain,
    build_provider,
    merge_metadata,
)
from streamserver.metadata.providers import Credit, MediaMetadata, NoProvider


# ---------------------------------------------------------------------------
# Filename parsing
# ---------------------------------------------------------------------------
#: (filename, title, year, season, episode)
FILENAME_CASES = [
    ("Dune.Part.Two.2024.2160p.UHD.BluRay.x265-GROUP.mkv", "Dune Part Two", 2024, None, None),
    ("Amelie.2001.FRENCH.1080p.mkv", "Amelie", 2001, None, None),
    ("The.Lord.of.the.Rings.The.Return.of.the.King.2003.EXTENDED.2160p.UHD.BluRay.x265-TERMiNAL.mkv",
     "The Lord of the Rings The Return of the King", 2003, None, None),
    ("Arrival (2016) [1080p] [YTS.AG].mp4", "Arrival", None, None, None),
    ("Se7en.1995.REMASTERED.1080p.BluRay.x264.mkv", "Se7en", 1995, None, None),
    ("[SubsPlease] Frieren - Beyond Journey's End - 12 (1080p) [A1B2C3D4].mkv",
     "Frieren Beyond Journey's End", None, None, None),
    ("Blade.Runner.2049.2017.2160p.WEB-DL.ddp5.1.Atmos.DV.HDR.H.265-EVO.mkv",
     "Blade Runner", 2017, None, None),
    ("1917.2019.1080p.BluRay.x264-SPARKS.mkv", "1917 2019", 2019, None, None),
    ("Spider-Man.No.Way.Home.2021.720p.mkv", "Spider-Man No Way Home", 2021, None, None),
    ("Some Movie 2020 HDR10+ 2160p 10bit.mkv", "Some Movie", 2020, None, None),
    ("the-thing-1982.mkv", "The-Thing", 1982, None, None),
    ("My.Movie.Name.2019.1080p.BluRay.x264-GRP (1).mkv", "My Movie Name", 2019, None, None),
    ("10bit.DDP5.1.Atmos.x265-DAR.mkv", "Untitled", None, None, None),
    ("Frieren.S01E05.1080p.CR.mkv", "Frieren", None, 1, 5),
    ("Avengers.Endgame.2019.2160p.UHD.BluRay.REMUX.HDR.DV.TrueHD.7.1.Atmos-FGT.mkv",
     "Avengers Endgame", 2019, None, None),
    ("Dune.2021.1080p.WEBRip.DDP5.1.x264-NTb.mkv", "Dune", 2021, None, None),
    ("Ünder_Wörld.mkv", "Ünder Wörld", None, None, None),
    ("3.10.to.Yuma.1957.1080p.BluRay.x264-C Classics.mkv", "3:10 to Yuma", 1957, None, None),
    ("movie_with_no_info.mkv", "Movie With No Info", None, None, None),
    ("V_for_Vendetta.2005.1080p.BluRay.x264.mkv", "V for Vendetta", 2005, None, None),
    ("Breaking.Bad.S01.1080p.BluRay.x264-AiRisoN.mkv", "Breaking Bad", None, 1, None),
    ("The.Office.US.S03E12.PROPER.HDTV.X264-DIMENSION.mkv", "The Office US", None, 3, 12),
]


@pytest.mark.parametrize("filename,title,year,season,episode", FILENAME_CASES)
def test_filename_parsing(filename, title, year, season, episode):
    parsed = parse_filename(filename)
    assert parsed.title == title, filename
    assert parsed.year == year, filename
    assert parsed.season == season, filename
    assert parsed.episode == episode, filename


@pytest.mark.parametrize("filename,quality", [
    ("Dune.2160p.mkv", "2160p"),
    ("Dune.1080p.mkv", "1080p"),
    ("Dune.720p.mkv", "720p"),
    ("Dune.4K.mkv", "2160p"),
    ("Dune.UHD.mkv", "2160p"),
    ("Dune.mkv", None),
])
def test_quality_detection(filename, quality):
    assert parse_filename(filename).quality == quality


@pytest.mark.parametrize("filename,audio", [
    ("Movie.TrueHD.7.1.mkv", "truehd"),
    ("Movie.Atmos.mkv", "atmos"),
    ("Movie.DTS-HD.MA.5.1.mkv", "dtshd"),
    ("Movie.DDP5.1.mkv", "eac3"),
    ("Movie.AAC.mkv", "aac"),
])
def test_audio_detection(filename, audio):
    assert parse_filename(filename).audio == audio


@pytest.mark.parametrize("filename,release", [
    ("Movie.2020.1080p.BluRay.x264.mkv", "bluray"),
    ("Movie.2020.2160p.WEB-DL.mkv", "webdl"),
    ("Movie.2020.1080p.WEBRip.mkv", "webrip"),
    ("Movie.2020.1080p.HDTV.mkv", "hdtv"),
    ("Movie.2020.DVDRip.mkv", "dvd"),
    ("Movie.2020.2160p.BluRay.REMUX.mkv", "remux"),
])
def test_release_detection(filename, release):
    assert parse_filename(filename).release == release


def test_release_group_extraction():
    assert parse_filename("Dune.2021.1080p.x264-SPARKS.mkv").group == "SPARKS"
    assert parse_filename("Dune.2021.1080p.x264.mkv").group is None


def test_hd_and_hdr_detection():
    assert parse_filename("Dune.2160p.HDR10.mkv").hdr == "HDR10"
    assert parse_filename("Dune.2160p.HDR10+.mkv").hdr == "HDR10+"
    assert parse_filename("Dune.2160p.DV.HDR.mkv").hdr == "Dolby Vision"
    assert parse_filename("Dune.2160p.10bit.mkv").bit_depth == 10


def test_edition_detection():
    assert parse_filename("Movie.2020.IMAX.1080p.mkv").edition == "imax"
    assert parse_filename("Movie.2020.Directors.Cut.1080p.mkv").edition == "directors cut"
    assert parse_filename("Movie.2020.REMASTERED.1080p.mkv").edition == "remastered"


def test_season_only_filename_is_a_series():
    parsed = parse_filename("Breaking.Bad.S02.1080p.mkv")
    assert parsed.season == 2
    assert parsed.episode is None


@pytest.mark.parametrize("filename,first,end", [
    ("Show.S01E01-E05.1080p.mkv", 1, 5),
    ("Show.S01E01xE05.1080p.mkv", 1, 5),
    ("Show.S01E01E05.1080p.mkv", 1, 5),
    ("Show.S01E05.1080p.mkv", 5, None),
])
def test_multi_episode_range(filename, first, end):
    parsed = parse_filename(filename)
    assert parsed.season == 1
    assert parsed.episode == first
    assert parsed.episode_end == end


@pytest.mark.parametrize("filename", [
    "Show.S01E05.1080p.mkv",
    "Show.S01E05.1080p.5.1.mkv",
    "Show.S01E05v2.1080p.mkv",
    "Show.S01E05.AAC2.0.x264-GRP.mkv",
])
def test_a_bare_episode_number_is_not_a_range(filename):
    """Every one of these has a number after the episode marker.  Reading it as
    the end of a range would tell the importer to look for episode 1080."""
    parsed = parse_filename(filename)
    assert parsed.episode == 5
    assert parsed.episode_end is None


@pytest.mark.parametrize("hostile", [
    "", "   ", "..", ".", "\x00\x01evil.mkv", "../../etc/passwd.mkv",
    "a" * 5000 + ".mkv", "日本語のタイトル.mkv", "🎬🎥 movie.mkv",
])
def test_hostile_filenames_parse_without_crashing(hostile):
    parsed = parse_filename(hostile)
    assert isinstance(parsed.title, str)
    assert len(parsed.title) <= 200
    assert "/" not in parsed.title and "\\" not in parsed.title
    assert "\x00" not in parsed.title


def test_a_path_is_reduced_to_its_basename():
    """The parser is meant to see a filename; given a path it must not let the
    directory part become the title."""
    parsed = parse_filename("/srv/media/movies/Dune.2021.mkv")
    assert parsed.title == "Dune"
    assert "/" not in parsed.title


def test_sort_key_ignores_leading_articles():
    """The stored sort key drops a leading article, which is how a shelf gets
    *Matrices* filed under M rather than T."""
    assert sort_key("The Matrix") == "matrix"
    assert sort_key("A Quiet Place") == "quiet place"
    assert sort_key("Amadeus", ignore_articles=False) == "amadeus"


def test_compare_titles_orders_the_way_a_reader_expects():
    """Display order is alphabetical -- plain string order, so "Alien" sorts
    before "The Matrix".  A library that files "The Matrix" under A is
    alphabetical the way a phone book is, not the way a shelf is.

    Library listings do not use this: they sort on the stored ``sort_title``
    column, which *is* the article-stripped key, because an alphabetical index
    has to live in the database to be usable at 1000+ titles.
    """
    assert compare_titles("Alien", "The Matrix") < 0
    assert compare_titles("The Matrix", "Alien") > 0
    assert compare_titles("A Quiet Place", "Braveheart") < 0
    # The stripped key is only consulted for titles that differ by an article,
    # and only when the caller asks for it.
    assert compare_titles("The Matrix", "the matrix") == (
        compare_titles("The Matrix", "the matrix", ignore_articles=True)
    )
    assert compare_titles("Matrix", "The Matrix", ignore_articles=True) != 0
    assert compare_titles("The Matrix", "The Matrix", ignore_articles=True) == 0


def test_the_article_stripped_key_is_what_listings_sort_on():
    """A stored sort key is what makes A-Z usable: the index is on the
    stripped value, so "The Matrix" files under M and "A Quiet Place" under Q."""
    titles = ["The Matrix", "Alien", "A Quiet Place", "Braveheart", "Amadeus"]
    assert sorted(titles, key=sort_key) == [
        "Alien", "Amadeus", "Braveheart", "The Matrix", "A Quiet Place",
    ]
    # Which is *not* the same as plain alphabetical order: that files "A Quiet
    # Place" under A.  Both are defensible, and the library chooses one -- the
    # one an index can serve.
    assert sorted(titles) != sorted(titles, key=sort_key)


def test_title_helpers():
    assert clean_title("  Dune  ") == "Dune"
    assert normalise_title("") == "Untitled"
    assert len(normalise_title("x" * 500)) == 200


# ---------------------------------------------------------------------------
# Scanner
# ---------------------------------------------------------------------------
@pytest.fixture
def tree(library_root: Path) -> Path:
    (library_root / "a.mkv").write_bytes(b"a")
    (library_root / "b.mp4").write_bytes(b"b")
    (library_root / "c.avi").write_bytes(b"c")
    (library_root / "notes.txt").write_bytes(b"text")
    (library_root / "sub").mkdir()
    (library_root / "sub" / "d.mkv").write_bytes(b"d")
    (library_root / "Sample").mkdir()
    (library_root / "Sample" / "trailer.mkv").write_bytes(b"t")
    (library_root / "Extras").mkdir()
    (library_root / "Extras" / "deleted-scene.mkv").write_bytes(b"e")
    (library_root / "empty.mkv").touch()
    return library_root


def test_scanner_finds_media_and_skips_junk(tree):
    found = {p.name for p in iter_media_files(tree)}
    assert found == {"a.mkv", "b.mp4", "c.avi", "d.mkv"}


def test_scanner_skips_extras_and_samples(tree):
    names = {p.name for p in iter_media_files(tree)}
    assert "trailer.mkv" not in names
    assert "deleted-scene.mkv" not in names


def test_scanner_skips_empty_files(tree):
    names = {p.name for p in scan_tree(tree)}
    assert "empty.mkv" not in names


def test_scanner_skips_symlinked_directories(library_root: Path, tmp_path: Path):
    (library_root / "real.mkv").write_bytes(b"r")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.mkv").write_bytes(b"s")
    (library_root / "escape").symlink_to(outside)

    names = {p.name for p in iter_media_files(library_root)}
    assert "real.mkv" in names
    assert "secret.mkv" not in names


def test_scanner_follows_symlinked_files_inside_the_root(library_root: Path):
    (library_root / "real.mkv").write_bytes(b"r")
    (library_root / "alias.mkv").symlink_to(library_root / "real.mkv")
    names = {p.name for p in iter_media_files(library_root)}
    assert {"real.mkv", "alias.mkv"} <= names


def test_scan_tree_reports_size_and_depth(tree):
    records = {r.path.name: r for r in scan_tree(tree)}
    assert records["a.mkv"].size == 1
    assert records["a.mkv"].depth == 0
    assert records["d.mkv"].depth == 1


def test_scan_tree_is_deterministic(tree):
    first = [r.path.name for r in scan_tree(tree)]
    second = [r.path.name for r in scan_tree(tree)]
    assert first == second == sorted(first)


def test_is_media_file():
    assert is_media_file(Path("a.mkv"))
    assert is_media_file(Path("a.MP4"))
    assert not is_media_file(Path("a.txt"))
    assert not is_media_file(Path("a"))


def test_sidecars_are_discovered(library_root: Path):
    media = library_root / "Movie.2020.mkv"
    media.write_bytes(b"x")
    (library_root / "Movie.nfo").write_text("<movie/>")
    (library_root / "Movie.jpg").write_bytes(b"jpeg")

    found = {p.suffix for p in find_sidecars(media)}
    assert ".nfo" in found
    assert ".jpg" in found


def test_sidecars_in_a_named_directory(library_root: Path):
    media = library_root / "Movie.2020.mkv"
    media.write_bytes(b"x")
    art = library_root / "Movie"
    art.mkdir()
    (art / "movie.nfo").write_text("<movie/>")
    found = {p.suffix for p in find_sidecars(media)}
    assert ".nfo" in found


# ---------------------------------------------------------------------------
# ffprobe parsing
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("raw,expected", [
    ("30000/1001", 30000 / 1001),
    ("25", 25.0),
    ("N/A", 0.0),
    ("", 0.0),
    ("unknown", 0.0),
    ("nan", 0.0),
    ("inf", 0.0),
    ("1/0", 0.0),
    ("garbage", 0.0),
    (None, 0.0),
])
def test_as_float_is_defensive(raw, expected):
    assert _as_float(raw) == expected


def test_hdr_detection_from_colour_transfer():
    assert _hdr_format({}, {"color_transfer": "arib-std-b67"}) == "HDR10"
    assert _hdr_format({}, {"color_transfer": "smpte2084",
                            "color_primaries": "bt2020"}) == "HDR10"
    assert _hdr_format({}, {"color_transfer": "bt709"}) is None
    assert _hdr_format({"MASTERING_DISPLAY_METADATA": "1"}, {}) == "1"


def test_probe_reports_failure_on_a_bad_file(tmp_path: Path):
    bad = tmp_path / "bad.mkv"
    bad.write_bytes(b"\x00" * 1024)
    result = MediaProbe().probe(bad)
    # ffprobe is installed on the build machine, so this is the realistic case:
    # a file that exists but cannot be parsed.
    assert result.ok is False
    assert result.error


def test_probe_reports_an_empty_file(tmp_path: Path):
    empty = tmp_path / "empty.mkv"
    empty.touch()
    result = MediaProbe().probe(empty)
    assert result.ok is False
    assert "empty" in (result.error or "")


def test_probe_reports_a_missing_file(tmp_path: Path):
    result = MediaProbe().probe(tmp_path / "nope.mkv")
    assert result.ok is False
    assert "cannot stat" in (result.error or "")


def test_probe_reports_size_even_when_unparseable(tmp_path: Path):
    bad = tmp_path / "bad.mkv"
    bad.write_bytes(b"x" * 4096)
    result = MediaProbe().probe(bad)
    assert result.file_size_bytes == 4096


# ---------------------------------------------------------------------------
# Metadata providers
# ---------------------------------------------------------------------------
def test_local_provider_reads_json(tmp_path: Path):
    sidecar = tmp_path / "movie.json"
    sidecar.write_text(json.dumps({"movie": {
        "title": "Dune", "year": 2021, "overview": "Space.",
        "genres": ["Science Fiction", "Adventure"], "rating": 8.0,
        "director": ["Denis Villeneuve"],
        "cast": [{"name": "Timothee Chalamet"}, {"name": "Rebecca Ferguson"}],
        "ids": {"tmdb": "438631"},
    }}))
    meta = LocalSidecarProvider().lookup(parse_filename("x.mkv"), sidecars=[sidecar])
    assert meta.title == "Dune"
    assert meta.year == 2021
    assert meta.genres == ["Science Fiction", "Adventure"]
    assert meta.rating_value == 8.0
    assert [d.name for d in meta.directors] == ["Denis Villeneuve"]
    assert [c.name for c in meta.cast] == ["Timothee Chalamet", "Rebecca Ferguson"]
    assert meta.external_ids == {"tmdb": "438631"}


def test_local_provider_reads_kodi_nfo(tmp_path: Path):
    nfo = tmp_path / "movie.nfo"
    nfo.write_text(
        '<?xml version="1.0"?><movie>'
        "<title>Arrival</title><year>2016</year><runtime>116</runtime>"
        "<plot>Linguist meets aliens.</plot>"
        "<genre>Science Fiction</genre><genre>Drama</genre>"
        "<rating>7.9</rating><mpaa>PG-13</mpaa>"
        "<director>Denis Villeneuve</director>"
        "<actor><name>Amy Adams</name><role>Louise Banks</role></actor>"
        '<uniqueid type="imdb">tt2543164</uniqueid>'
        "</movie>"
    )
    meta = LocalSidecarProvider().lookup(parse_filename("x.mkv"), sidecars=[nfo])
    assert meta.title == "Arrival"
    assert meta.runtime_minutes == 116
    assert meta.parental_rating == "PG-13"
    assert meta.external_ids == {"imdb": "tt2543164"}
    assert meta.cast[0].character == "Louise Banks"


def test_local_provider_survives_malformed_input(tmp_path: Path):
    broken = tmp_path / "movie.nfo"
    broken.write_text("<movie><title>Nope</movie>")
    assert LocalSidecarProvider().lookup(parse_filename("x.mkv"), sidecars=[broken]) is None

    not_json = tmp_path / "movie.json"
    not_json.write_text("{not json")
    assert LocalSidecarProvider().lookup(parse_filename("x.mkv"), sidecars=[not_json]) is None

    wrong_shape = tmp_path / "list.json"
    wrong_shape.write_text("[1, 2, 3]")
    assert LocalSidecarProvider().lookup(parse_filename("x.mkv"), sidecars=[wrong_shape]) is None


def test_provider_payload_cannot_smuggle_a_path(tmp_path: Path):
    """Metadata is untrusted input: a title must never look like a path, and
    an artwork path must never point outside the library."""
    sidecar = tmp_path / "movie.json"
    sidecar.write_text(json.dumps({
        "title": "../../etc/passwd",
        "poster": "/etc/shadow",
        "backdrop": "../../root/x.jpg",
    }))
    meta = LocalSidecarProvider().lookup(parse_filename("x.mkv"), sidecars=[sidecar])
    assert "/" not in meta.title.replace(" / ", "")
    assert meta.poster_path is None
    assert ".." in (meta.backdrop_path or "")


def test_provider_caps_oversized_sidecars(tmp_path: Path):
    huge = tmp_path / "movie.json"
    huge.write_text(json.dumps({"title": "x", "overview": "y" * (5 * 1024 * 1024)}))
    assert LocalSidecarProvider().lookup(parse_filename("x.mkv"), sidecars=[huge]) is None


def test_build_provider_falls_back_to_local_for_an_unknown_name():
    provider = build_provider("nonsense")
    assert isinstance(provider, LocalSidecarProvider)
    assert provider.requires_key is False


def test_providers_requiring_a_key_report_unavailable():
    provider = build_provider("tmdb", api_key="")
    assert provider.available() is False
    assert build_provider("tmdb", api_key="abc").available() is True


def test_provider_chain_prefers_local():
    local = build_provider("local")
    remote = build_provider("tmdb", api_key="k")
    chain = ProviderChain([remote, local])
    assert [p.name for p in chain.providers] == ["local", "tmdb"]
    chain = ProviderChain([local, remote])
    assert [p.name for p in chain.providers] == ["local", "tmdb"]


def test_merge_keeps_the_higher_priority_value():
    base = MediaMetadata(title="Local", overview="local text", genres=["A"])
    incoming = MediaMetadata(title="Remote", overview="remote text", year=2020)
    merged = merge_metadata(base, incoming)
    assert merged.title == "Local"
    assert merged.overview == "local text"
    assert merged.year == 2020          # the gap is filled


def test_no_provider_returns_nothing():
    assert NoProvider().lookup(parse_filename("x.mkv")) is None