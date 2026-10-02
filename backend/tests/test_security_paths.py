"""Path containment.

These are the tests that matter most in the whole suite: they decide whether a
crafted path can read a file the server was never given.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from streamserver.security.paths import (
    MediaRootRegistry,
    PathViolation,
    safe_join,
    sanitize_filename,
    slugify,
)


@pytest.fixture
def root(tmp_path: Path) -> Path:
    path = tmp_path / "library"
    (path / "movies").mkdir(parents=True)
    (path / "movies" / "a.mkv").write_bytes(b"x")
    (path / "movies" / "sub").mkdir()
    (path / "movies" / "sub" / "b.mkv").write_bytes(b"y")
    (tmp_path / "outside").mkdir()
    (tmp_path / "outside" / "secret.txt").write_text("classified")
    return path


@pytest.fixture
def registry(root: Path) -> MediaRootRegistry:
    return MediaRootRegistry((root,))


# ---------------------------------------------------------------------------
# Containment
# ---------------------------------------------------------------------------
def test_file_inside_the_root_is_allowed(registry, root):
    assert registry.resolve(root / "movies" / "a.mkv").name == "a.mkv"
    assert registry.resolve(root / "movies" / "sub" / "b.mkv").name == "b.mkv"
    assert registry.contains(root / "movies" / "a.mkv")


def test_the_root_itself_is_allowed(registry, root):
    assert registry.resolve(root) == root.resolve()


@pytest.mark.parametrize("escape", [
    "../../etc/passwd",
    "/etc/passwd",
    "/etc/shadow",
    "/root/.ssh/id_rsa",
    "/proc/self/environ",
    "/var/lib/streamserver/streamserver.db",
])
def test_paths_outside_the_root_are_refused(registry, escape):
    with pytest.raises(PathViolation):
        registry.resolve(escape)


def test_traversal_that_returns_inside_is_still_contained(registry, root):
    """A path that climbs out and back is fine -- only the result matters."""
    sneaky = root / "movies" / ".." / ".." / "outside" / "secret.txt"
    with pytest.raises(PathViolation):
        registry.resolve(sneaky)


def test_nul_byte_is_refused(registry, root):
    with pytest.raises(PathViolation, match="NUL"):
        registry.resolve(str(root / "movies" / "a.mkv") + "\x00.txt")


def test_symlink_escape_is_refused(registry, root, tmp_path):
    link = root / "movies" / "escape"
    link.symlink_to(tmp_path / "outside")
    with pytest.raises(PathViolation):
        registry.resolve(link / "secret.txt")


def test_symlink_inside_the_root_is_allowed(registry, root):
    """A symlink that stays inside the library is a legitimate organiser's tool."""
    link = root / "movies" / "shortcut"
    link.symlink_to(root / "movies" / "a.mkv")
    assert registry.resolve(link).name == "a.mkv"


def test_nested_roots_are_deduplicated(tmp_path):
    outer = tmp_path / "media"
    inner = outer / "movies"
    inner.mkdir(parents=True)
    registry = MediaRootRegistry((outer, inner))
    assert len(registry.list_roots()) == 1


def test_empty_registry_refuses_everything(tmp_path):
    registry = MediaRootRegistry(())
    assert registry.is_empty
    with pytest.raises(PathViolation, match="no library roots"):
        registry.resolve(tmp_path / "anything.mkv")


def test_relative_root_is_refused():
    with pytest.raises(PathViolation, match="absolute"):
        MediaRootRegistry(("relative/dir",))


def test_resolve_existing_requires_a_regular_file(registry, root):
    assert registry.resolve_existing(root / "movies" / "a.mkv").name == "a.mkv"
    with pytest.raises(PathViolation, match="does not exist"):
        registry.resolve_existing(root / "movies" / "nope.mkv")
    with pytest.raises(PathViolation, match="not a regular file"):
        registry.resolve_existing(root / "movies" / "sub")


def test_a_fifo_is_not_a_regular_file(registry, root):
    """A device/FIFO at the media path must never be streamed."""
    import os

    fifo = root / "movies" / "pipe.mkv"
    try:
        os.mkfifo(fifo)
    except (AttributeError, OSError):
        pytest.skip("mkfifo unavailable")
    with pytest.raises(PathViolation, match="not a regular file"):
        registry.resolve_existing(fifo)


def test_relativize_returns_a_stable_key(registry, root):
    key = registry.relativize(root / "movies" / "sub" / "b.mkv")
    assert key == "movies/sub/b.mkv"
    with pytest.raises(PathViolation):
        registry.relativize("/etc/passwd")


def test_root_for_identifies_the_owning_root(tmp_path):
    first = tmp_path / "movies"
    second = tmp_path / "anime"
    first.mkdir()
    second.mkdir()
    (second / "ep.mkv").write_bytes(b"x")
    registry = MediaRootRegistry((first, second))
    assert registry.root_for(second / "ep.mkv") == second.resolve()


# ---------------------------------------------------------------------------
# Filenames
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("hostile", [
    "../../etc/passwd", "..\\..\\windows\\system32", "/etc/passwd",
    "a/b/c", "sub\\dir", "a\\b\\c",
])
def test_sanitize_rejects_anything_with_a_path(hostile):
    with pytest.raises(PathViolation):
        sanitize_filename(hostile)


def test_sanitize_keeps_ordinary_names():
    assert sanitize_filename("poster.jpg") == "poster.jpg"
    assert sanitize_filename("  spaced  name.png ") == "spaced  name.png"
    assert sanitize_filename("a<b>c.jpg") == "abc.jpg"


def test_sanitize_reduces_to_a_bare_name():
    """``.``, ``..`` and empty input carry no usable name, so they collapse to
    the fallback rather than raising -- there is nothing suspicious to reject,
    only nothing to work with."""
    for unusable in ("", "   ", "...", ".", ".."):
        cleaned = sanitize_filename(unusable)
        assert cleaned == "file"
        assert "/" not in cleaned and "\\" not in cleaned


def test_sanitize_caps_length():
    assert len(sanitize_filename("x" * 400 + ".jpg")) <= 200


def test_sanitize_rejects_nul():
    with pytest.raises(PathViolation, match="NUL"):
        sanitize_filename("evil\x00.jpg")


def test_safe_join_confines(tmp_path):
    base = tmp_path / "images"
    base.mkdir()
    assert safe_join(base, "posters", "a.jpg") == (base / "posters" / "a.jpg")
    with pytest.raises(PathViolation):
        safe_join(base, "../..")


def test_safe_join_refuses_symlinked_escape(tmp_path):
    base = tmp_path / "images"
    base.mkdir()
    (tmp_path / "outside").mkdir()
    (base / "link").symlink_to(tmp_path / "outside")
    with pytest.raises(PathViolation):
        safe_join(base, "link", "a.jpg")


# ---------------------------------------------------------------------------
# Slugs
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("value,expected", [
    ("Dune", "dune"),
    ("Amélie", "amelie"),
    ("Le Fabuleux Destin", "le-fabuleux-destin"),
    ("Spider-Man: No Way Home", "spider-man-no-way-home"),
    ("2001: A Space Odyssey", "2001-a-space-odyssey"),
    ("!!!", "item"),
    ("", "item"),
])
def test_slugify(value, expected):
    assert slugify(value) == expected


def test_slugify_caps_length():
    assert len(slugify("word " * 200, max_length=40)) <= 40


def test_slugify_never_contains_a_separator():
    for value in ("a/b", "a\\b", "a:b", "a*b", "a?b"):
        assert "/" not in slugify(value)
        assert "\\" not in slugify(value)