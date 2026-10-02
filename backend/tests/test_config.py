"""Configuration loading and validation."""

from __future__ import annotations

from pathlib import Path

import pytest

from streamserver.config import (
    Config,
    ConfigError,
    dump_config,
    load_config,
)


def test_defaults_are_sane():
    config = load_config(environ={})
    assert config.server.host == "127.0.0.1"
    assert config.server.port == 8484
    assert config.server.cors_origins == []          # CORS off by default
    assert config.auth.access_token_ttl_seconds < config.auth.refresh_token_ttl_seconds
    assert config.metadata.default_provider == "local"  # offline by default
    assert config.media.library_roots == []


def test_toml_file_is_read(tmp_path: Path):
    path = tmp_path / "server.toml"
    path.write_text(
        """
        [server]
        host = "0.0.0.0"
        port = 9000

        [media]
        library_roots = ["/srv/movies", "/srv/anime"]

        [auth]
        password_min_length = 12
        """,
        encoding="utf-8",
    )
    config = load_config(path, environ={})
    assert config.server.host == "0.0.0.0"
    assert config.server.port == 9000
    assert config.media.library_roots == ["/srv/movies", "/srv/anime"]
    assert config.auth.password_min_length == 12
    assert config.source_path == path


def test_environment_overrides_file(tmp_path: Path):
    path = tmp_path / "server.toml"
    path.write_text('[server]\nport = 9000\n', encoding="utf-8")
    config = load_config(path, environ={
        "STREAMSERVER_SERVER_PORT": "9999",
        "STREAMSERVER_STORAGE_DATA_DIR": "/tmp/streamserver-env-test",
        "STREAMSERVER_MEDIA_LIBRARY_ROOTS": "/a,/b",
    })
    assert config.server.port == 9999
    assert config.storage.data_dir == Path("/tmp/streamserver-env-test")
    assert config.media.library_roots == ["/a", "/b"]


def test_explicit_overrides_beat_environment(tmp_path: Path):
    config = load_config(
        environ={"STREAMSERVER_SERVER_PORT": "9999"},
        overrides={"server.port": 1234},
    )
    assert config.server.port == 1234


@pytest.mark.parametrize("overrides,expected", [
    ({"server.port": 999999}, "out of range"),
    ({"server.port": 0}, "out of range"),
    ({"auth.password_min_length": 3}, "at least 8"),
    ({"media.stream_chunk_size": 10}, "at least 64 KiB"),
    ({"metadata.default_provider": "nope"}, "unknown metadata provider"),
    ({"storage.data_dir": "relative/path"}, "must be absolute"),
    ({"server.allow_insecure_transport": "maybe"}, "expected a boolean"),
])
def test_validation_rejects_nonsense(overrides, expected):
    with pytest.raises(ConfigError, match=expected):
        load_config(overrides=overrides, environ={})


def test_unknown_key_is_rejected(tmp_path: Path):
    path = tmp_path / "server.toml"
    path.write_text('[server]\nnot_a_real_key = 1\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="unknown configuration key"):
        load_config(path, environ={})


def test_unknown_section_is_rejected(tmp_path: Path):
    path = tmp_path / "server.toml"
    path.write_text("[nonsense]\nvalue = 1\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="unknown section"):
        load_config(path, environ={})


def test_invalid_toml_is_reported_with_the_path(tmp_path: Path):
    path = tmp_path / "server.toml"
    path.write_text("[server\nport = 1\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="invalid TOML"):
        load_config(path, environ={})


def test_access_token_must_expire_before_the_refresh_token():
    with pytest.raises(ConfigError, match="must be shorter than"):
        load_config(overrides={
            "auth.access_token_ttl_seconds": 900,
            "auth.refresh_token_ttl_seconds": 600,
        }, environ={})


def test_relative_library_root_is_rejected():
    config = load_config(overrides={"media.library_roots": ["relative/path"]}, environ={})
    with pytest.raises(ConfigError, match="must be absolute"):
        config.library_root_paths()


def test_bad_host_is_rejected():
    with pytest.raises(ConfigError, match="not a valid address"):
        load_config(overrides={"server.host": "http://evil/ -- x"}, environ={})


def test_round_trip_through_dump(tmp_path: Path):
    """dump_config writes values quoted, which is valid TOML for scalars too.

    This locks in that behaviour: it is what lets a hand-edited config file
    quote every value without changing how the server reads it.
    """
    original = load_config(overrides={
        "server.port": 4321,
        "server.allow_insecure_transport": True,
        "media.library_roots": ["/srv/movies"],
        "storage.data_dir": str(tmp_path / "d"),
        "storage.database_path": str(tmp_path / "d" / "custom.db"),
        "storage.image_dir": str(tmp_path / "d" / "art"),
    }, environ={})
    path = tmp_path / "out.toml"
    path.write_text(dump_config(original), encoding="utf-8")

    reloaded = load_config(path, environ={})
    assert reloaded.server.port == 4321
    assert reloaded.server.allow_insecure_transport is True
    assert reloaded.media.library_roots == ["/srv/movies"]
    assert reloaded.storage.data_dir == tmp_path / "d"
    assert reloaded.database_file == tmp_path / "d" / "custom.db"
    assert reloaded.image_path == tmp_path / "d" / "art"


@pytest.mark.parametrize("value,expected", [
    ("true", True), ("1", True), ("yes", True), ("on", True),
    ("off", False), ("0", False), ("no", False), (1, True), (0, False),
    (True, True), (False, False),
])
def test_bool_coercion_accepts_the_usual_spellings(value, expected):
    config = load_config(overrides={"server.allow_insecure_transport": value},
                         environ={})
    assert config.server.allow_insecure_transport is expected


def test_unrecognised_bool_is_rejected_rather_than_guessed():
    with pytest.raises(ConfigError, match="expected a boolean"):
        load_config(overrides={"server.allow_insecure_transport": "nope"}, environ={})


def test_required_file_missing_raises(tmp_path: Path):
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "absent.toml", required=True, environ={})


def test_ensure_directories_creates_everything(tmp_path: Path):
    config = load_config(overrides={"storage.data_dir": str(tmp_path / "d")}, environ={})
    config.ensure_directories()
    assert config.database_file.parent.is_dir()
    assert config.image_path.is_dir()
    assert config.log_path.is_dir()
    assert config.backup_path.is_dir()