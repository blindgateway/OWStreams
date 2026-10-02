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


#: The on-disk media directory.  The fixture that *imports* titles into it is
#: ``imported``; keeping the two distinctly named avoids a shadowed fixture
#: turning the app fixture recursive.
@pytest.fixture
def library_root(tmp_path: Path) -> Path:
    path = tmp_path / "movies"
    path.mkdir()
    return path


@pytest.fixture
def config(data_dir: Path, library_root: Path) -> Config:
    """A configuration with cheap KDF parameters.

    scrypt at production cost is ~100 ms, which would dominate the runtime of a
    few hundred password verifications.  The tests that care about the real
    cost check the *parameters*, not the wall clock.
    """
    cfg = load_config(
        overrides={
            "storage.data_dir": str(data_dir),
            "storage.log_to_file": False,
            "media.library_roots": [str(library_root)],
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
def make_media_file(library_root: Path):
    """Create a file with plausible media bytes and return its path."""
    counter = {"n": 0}

    def _make(name: str = "", size: int = 4096) -> Path:
        counter["n"] += 1
        filename = name or f"Movie.{1990 + counter['n']}.1080p.mkv"
        path = library_root / filename
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
    """An unauthenticated test client.

    Each test that needs an account calls :func:`perform_setup` first (directly
    on the app's context, so it does not depend on request ordering); the
    ``owner`` fixture below runs it for the tests that want a signed-in admin.
    """
    from starlette.testclient import TestClient

    with TestClient(app, base_url="http://localhost") as test_client:
        yield test_client


def perform_setup(app, username: str = ADMIN_USERNAME,
                  password: str = ADMIN_PASSWORD, **extra) -> dict:
    """Create the first administrator account through the service layer.

    Equivalent to ``POST /api/v1/auth/setup`` but without the HTTP round trip,
    so a test that is about *authentication* does not first have to get
    *authentication working*.
    """
    from streamserver.db import repositories as repo
    from streamserver.security import passwords

    context = app.state.context
    db = context.db
    if repo.get_user_by_username(db, username):
        raise AssertionError(f"{username} already exists; each test needs a clean app")

    config = context.config
    user = repo.create_user(
        db, username=username,
        password_hash=passwords.hash_password(
            password, n=config.auth.scrypt_n, r=config.auth.scrypt_r,
            p=config.auth.scrypt_p, maxmem=config.auth.scrypt_maxmem,
        ),
        is_admin=True, display_name=extra.get("display_name") or username,
        must_change_password=extra.get("must_change_password", False),
    )
    repo.create_profile(db, int(user["id"]), extra.get("profile_name") or "Main")
    return user


@pytest.fixture
def setup(app):
    """Create the first administrator account.  Returns its row."""
    return perform_setup(app)


@pytest.fixture
def owner(client, setup):
    """An administrator account with a live token.

    Sign-in is still exercised here -- but deliberately *after* the account is
    known to exist, so a failure in this fixture points at the login flow
    rather than at the setup path underneath it.
    """
    response = client.post(
        "/api/v1/auth/login",
        json={"username": setup["username"], "password": ADMIN_PASSWORD,
              "device_name": "pytest"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    return {
        "token": body["access_token"],
        "refresh": body["refresh_token"],
        "user_id": body["user"]["id"],
        "username": setup["username"],
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
def second_user(app, setup, config: Config):
    """A second, non-administrator account, used for the IDOR tests.

    Returns credentials plus a helper that signs that account in.
    """
    from streamserver.db import repositories as repo
    from streamserver.security import passwords

    db = app.state.context.db
    user = repo.create_user(
        db, username="bob",
        password_hash=passwords.hash_password(
            USER_PASSWORD, n=config.auth.scrypt_n, r=config.auth.scrypt_r,
            p=config.auth.scrypt_p,
        ),
        is_admin=False, display_name="Bob",
    )
    repo.create_profile(db, int(user["id"]), "Bob")

    return {
        "username": "bob",
        "password": USER_PASSWORD,
        "user_id": int(user["id"]),
    }


@pytest.fixture
def second_session(client, second_user):
    """``second_user`` signed in: headers plus its profile id."""
    response = client.post("/api/v1/auth/login", json={
        "username": second_user["username"], "password": second_user["password"],
    })
    assert response.status_code == 200, response.text
    token = response.json()["access_token"]
    auth = {"Authorization": f"Bearer {token}"}

    profiles = client.get("/api/v1/profiles", headers=auth).json()
    assert profiles
    profile_id = profiles[0]["id"]
    return {
        **second_user,
        "token": token,
        "profile_id": profile_id,
        "auth": auth,
        "headers": {**auth, "X-Profile-Id": str(profile_id)},
    }


@pytest.fixture
def capped(app, library_root: Path) -> dict:
    """A signed-in *non-administrator* account that still owes a password change.

    This is the account the default-password gate exists for, and it is not the
    administrator on purpose: the gate exempts administrators, so a fixture
    built on ``owner`` would pass whether the gate was wired up or not.

    Returns headers carrying the profile id, the profile id, and the first media
    id, so a test can mutate something real rather than assert on a 403 alone.
    """
    from streamserver.db import repositories as repo
    from streamserver.security import passwords

    context = app.state.context
    db = context.db
    config = context.config

    user = repo.create_user(
        db, username="capped",
        password_hash=passwords.hash_password(
            USER_PASSWORD, n=config.auth.scrypt_n, r=config.auth.scrypt_r,
            p=config.auth.scrypt_p, maxmem=config.auth.scrypt_maxmem,
        ),
        is_admin=False, display_name="Capped",
        must_change_password=True,
    )
    repo.create_profile(db, int(user["id"]), "Capped")

    # One title, so the mutation targets are real.
    (library_root / "Capped.2019.1080p.mkv").write_bytes(bytes(range(256)) * 16)
    stats = context.importer.scan()
    assert stats.failed == 0, stats.errors
    row = repo.get_media_by_path(
        db, str((library_root / "Capped.2019.1080p.mkv").resolve()))
    assert row is not None
    media_id = int(row["id"])

    return {
        "user_id": int(user["id"]),
        "username": "capped",
        "password": USER_PASSWORD,
        "profile_id": repo.list_profiles(db, int(user["id"]))[0]["id"],
        "media_id": media_id,
        "path": "/Capped.2019.1080p.mkv",
    }


@pytest.fixture
def capped_session(client, capped):
    """``capped`` signed in, with headers a caller can hand straight to a route."""
    response = client.post("/api/v1/auth/login", json={
        "username": capped["username"], "password": capped["password"],
    })
    assert response.status_code == 200, response.text
    assert response.json()["must_change_password"] is True
    token = response.json()["access_token"]
    auth = {"Authorization": f"Bearer {token}"}
    return {
        **capped,
        "token": token,
        "auth": auth,
        "headers": {**auth, "X-Profile-Id": str(capped["profile_id"])},
    }


@pytest.fixture
def library(app, library_root: Path) -> list[dict]:
    """Three imported titles with genres, so list/search routes have data.

    Depends only on ``app``: the files are written and imported through the
    service layer, so no account or profile needs to exist for a library test.
    """
    from streamserver.db import repositories as repo

    # Written here rather than through make_media_file so the fixture's own
    # inputs and the filenames it looks up afterwards cannot drift apart.
    for filename, size in (
        ("Dune.2021.1080p.WEBRip.DDP5.1.x264-NTb.mkv", 8192),
        ("Blade.Runner.2049.2017.2160p.WEB-DL.mkv", 8192),
        ("Arrival.2016.720p.mkv", 4096),
    ):
        (library_root / filename).write_bytes(bytes(range(256)) * (size // 256))

    # (filename, display title, year, genres)
    catalogue = [
        ("Dune.2021.1080p.WEBRip.DDP5.1.x264-NTb.mkv", "Dune", 2021,
         [("Science Fiction", "science-fiction"), ("Adventure", "adventure")]),
        ("Blade.Runner.2049.2017.2160p.WEB-DL.mkv", "Blade Runner", 2017,
         [("Science Fiction", "science-fiction")]),
        ("Arrival.2016.720p.mkv", "Arrival", 2016,
         [("Science Fiction", "science-fiction"), ("Drama", "drama")]),
    ]
    stats = app.state.context.importer.scan()
    assert stats.failed == 0, stats.errors

    items = []
    for filename, title, year, genre_list in catalogue:
        path = library_root / filename
        row = repo.get_media_by_path(app.state.context.db, str(path.resolve()))
        assert row is not None, f"{path} was not imported"
        app.state.context.importer.update_metadata(
            int(row["id"]), {"title": title, "year": year,
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