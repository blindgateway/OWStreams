"""Shared fixtures.

Every test gets its own data directory, database, signing key and library
tree.  Nothing is shared between tests, so they can run in any order and a
failure never leaves state behind for the next one.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from streamserver.config import Config, load_config  # noqa: E402
from streamserver.db.database import Database, migrate  # noqa: E402
from streamserver.db.migrations import MIGRATIONS  # noqa: E402

ADMIN_USERNAME = "phuree"
ADMIN_PASSWORD = "phuree-2024-secure"
USER_PASSWORD = "bob-2024-secure"


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    path = tmp_path / "data"
    path.mkdir()
    return path


@pytest.fixture
def library(tmp_path: Path) -> Path:
    path = tmp_path / "movies"
    path.mkdir()
    return path


@pytest.fixture
def config(data_dir: Path, library: Path) -> Config:
    """A configuration with cheap KDF parameters.

    scrypt at production cost is ~100 ms, which would dominate the runtime of a
    few hundred password verifications.  The tests that care about the real
    cost check the *parameters*, not the wall clock.
    """
    cfg = load_config(
        overrides={
            "storage.data_dir": str(data_dir),
            "storage.log_to_file": False,
            "media.library_roots": [str(library)],
            "auth.scrypt_n": 1024,          # 2**10: still a real KDF, ~1 ms
            "auth.scrypt_maxmem": 16 * 1024 * 1024,
            "server.host": "127.0.0.1",
        },
        environ={},
    )
    cfg.ensure_directories()
    return cfg


@pytest.fixture
def db(config: Config) -> Database:
    database = Database(config.database_file)
    migrate(database, MIGRATIONS)
    yield database
    database.close()


@pytest.fixture
def secret(config: Config) -> bytes:
    from streamserver.security.tokens import load_or_create_secret

    return load_or_create_secret(config.database_file)


@pytest.fixture
def make_media_file(library: Path):
    """Create a file with plausible media bytes and return its path."""
    counter = {"n": 0}

    def _make(name: str = "", size: int = 4096) -> Path:
        counter["n"] += 1
        filename = name or f"Movie.{1990 + counter['n']}.1080p.mkv"
        path = library / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(bytes(counter["n"] % 251 for _ in range(size)))
        return path

    return _make


@pytest.fixture
def app(config: Config):
    from streamserver.api.app import create_app

    application = create_app(config)
    yield application
    application.state.context.close()


@pytest.fixture
def client(app):
    from starlette.testclient import TestClient

    with TestClient(app, base_url="http://localhost") as test_client:
        yield test_client


@pytest.fixture
def owner(client):
    """An administrator account with a live token."""
    response = client.post(
        "/api/v1/auth/login",
        json={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD,
              "device_name": "pytest"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    return {
        "token": body["access_token"],
        "refresh": body["refresh_token"],
        "user_id": body["user"]["id"],
        "headers": {"Authorization": f"Bearer {body['access_token']}"},
    }


@pytest.fixture
def admin_profile(client, owner) -> dict:
    response = client.get("/api/v1/profiles", headers=owner["headers"])
    assert response.status_code == 200, response.text
    profiles = response.json()
    assert profiles, "the admin account should have a default profile"
    profile = profiles[0]
    return {
        "id": profile["id"],
        "headers": {**owner["headers"], "X-Profile-Id": str(profile["id"])},
    }


@pytest.fixture
def second_user(client, config: Config, db: Database):
    """A second, non-administrator account, used for the IDOR tests."""
    from streamserver.db import repositories as repo
    from streamserver.security import passwords

    if repo.get_user_by_username(db, "bob"):
        return {"username": "bob", "password": USER_PASSWORD}
    repo.create_user(
        db, username="bob",
        password_hash=passwords.hash_password(
            USER_PASSWORD, n=config.auth.scrypt_n, r=config.auth.scrypt_r,
            p=config.auth.scrypt_p,
        ),
        is_admin=False, display_name="Bob",
    )
    repo.create_profile(db, int(repo.get_user_by_username(db, "bob")["id"]), "Bob")
    db.close()
    return {"username": "bob", "password": USER_PASSWORD}


@pytest.fixture
def imported(app, make_media_file, client, admin_profile) -> list[dict]:
    """Three imported titles with genres, so list/search routes have data."""
    from streamserver.db import repositories as repo

    paths = [
        make_media_file("Dune.2021.1080p.WEBRip.DDP5.1.x264-NTb.mkv", 8192),
        make_media_file("Blade.Runner.2049.2017.2160p.WEB-DL.mkv", 8192),
        make_media_file("Arrival.2016.720p.mkv", 4096),
    ]
    titles = ["Dune", "Blade Runner", "Arrival"]
    genres = [
        [("Science Fiction", "science-fiction"), ("Adventure", "adventure")],
        [("Science Fiction", "science-fiction")],
        [("Science Fiction", "science-fiction"), ("Drama", "drama")],
    ]
    stats = app.state.context.importer.scan()
    assert stats.failed == 0, stats.errors

    items = []
    for path, title, genre_list in zip(paths, titles, genres):
        row = repo.get_media_by_path(app.state.context.db, str(path.resolve()))
        assert row is not None, f"{path} was not imported"
        app.state.context.importer.update_metadata(
            int(row["id"]), {"title": title, "year": int(path.name.split(".")[1]),
                             "overview": f"{title} overview.", "runtime_minutes": 120}
        )
        repo.set_media_genres(app.state.context.db, int(row["id"]), genre_list)
        repo.rebuild_search_index(app.state.context.db, int(row["id"]))
        items.append(repo.get_media(app.state.context.db, int(row["id"])))
    return items


@pytest.fixture
def autouse_scratch(monkeypatch):
    """Keep HOME and XDG inside the test tree so nothing escapes."""
    scratch = Path(os.environ.get("TMPDIR", "/tmp")) / "streamserver-tests"
    monkeypatch.setenv("HOME", str(scratch))
    yield