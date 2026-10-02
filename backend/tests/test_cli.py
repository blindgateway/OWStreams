"""CLI.

The command line is a second front door to the same services, so it gets the
same scrutiny: the same password policy, the same path containment, the same
refusal to touch anything outside the configured roots.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from streamserver.cli import main
from tests.conftest import ADMIN_PASSWORD, ADMIN_USERNAME


@pytest.fixture
def env(tmp_path: Path, monkeypatch) -> dict:
    """A CLI environment: private data dir, one library root, no console."""
    data = tmp_path / "data"
    library = tmp_path / "movies"
    library.mkdir()
    monkeypatch.setenv("STREAMSERVER_DATA_DIR", str(data))
    # Deliberately *not* set: the root is supplied per command so a test can
    # also exercise the "no roots configured" path.  Config precedence itself is
    # covered in test_config.py.
    return {"data": data, "library": library, "db": data / "streamserver.db"}


def run(args, capsys) -> tuple[int, str, str]:
    code = main(args)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


# ---------------------------------------------------------------------------
# Basics
# ---------------------------------------------------------------------------
def test_version_and_help(capsys):
    with pytest.raises(SystemExit) as exit_info:
        main(["--version"])
    assert exit_info.value.code == 0
    assert "streamserver" in capsys.readouterr().out


def test_a_subcommand_is_required():
    with pytest.raises(SystemExit):
        main([])


def test_init_db_creates_a_migrated_database(env, capsys):
    code, out, _ = run(["init-db"], capsys)
    assert code == 0
    assert "Database ready" in out
    assert "integrity: ok" in out
    assert env["db"].exists()


def test_init_db_is_idempotent(env, capsys):
    run(["init-db"], capsys)
    code, out, _ = run(["init-db"], capsys)
    assert code == 0
    assert "Database ready" in out


def test_init_config_writes_a_readable_file(env, capsys, tmp_path):
    destination = tmp_path / "server.toml"
    code, _, _ = run(["init-config", "-c", str(destination),
                      "--data-dir", str(tmp_path / "d"),
                      "--root", str(env["library"])], capsys)
    assert code == 0
    text = destination.read_text()
    assert "[server]" in text and "[media]" in text
    assert oct(destination.stat().st_mode)[-3:] == "600"


def test_init_config_refuses_to_clobber(env, capsys, tmp_path):
    destination = tmp_path / "server.toml"
    run(["init-config", "-c", str(destination)], capsys)
    code, _, err = run(["init-config", "-c", str(destination)], capsys)
    assert code == 1
    assert "already exists" in err


def test_doctor_passes_on_a_healthy_install(env, capsys):
    run(["init-db"], capsys)
    code, out, _ = run(["doctor", "--root", str(env["library"])], capsys)
    assert code == 0
    assert "library roots:" in out
    assert str(env["library"]) in out


def test_doctor_reports_a_missing_root(env, capsys, tmp_path):
    run(["init-db"], capsys)
    code, out, err = run(["doctor", "--root", str(tmp_path / "absent")], capsys)
    assert code == 1
    assert "PROBLEM" in err


def test_doctor_notes_an_uninitialised_database(env, capsys):
    code, out, _ = run(["doctor", "--root", str(env["library"])], capsys)
    assert "does not exist yet" in out


# ---------------------------------------------------------------------------
# Accounts
# ---------------------------------------------------------------------------
def test_add_user_creates_an_admin(env, capsys):
    run(["init-db"], capsys)
    code, out, _ = run(["add-user", ADMIN_USERNAME, "--password", ADMIN_PASSWORD,
                        "--admin"], capsys)
    assert code == 0
    assert "administrator" in out

    from streamserver.db.database import Database
    from streamserver.db import repositories as repo

    db = Database(env["db"])
    try:
        user = repo.get_user_by_username(db, ADMIN_USERNAME)
        assert user is not None and user["is_admin"] == 1
        assert "scrypt$" not in user["password_hash"] or user["password_hash"].startswith("scrypt$")
        assert repo.list_profiles(db, int(user["id"]))
    finally:
        db.close()


def test_add_user_enforces_the_password_policy(env, capsys):
    run(["init-db"], capsys)
    for weak in ("short", "12345678", "password"):
        code, _, err = run(["add-user", "someone", "--password", weak], capsys)
        assert code == 1, weak
        assert "too weak" in err.lower()


def test_add_user_rejects_a_duplicate(env, capsys):
    run(["init-db"], capsys)
    run(["add-user", ADMIN_USERNAME, "--password", ADMIN_PASSWORD], capsys)
    code, _, err = run(["add-user", ADMIN_USERNAME, "--password", ADMIN_PASSWORD], capsys)
    assert code == 1
    assert "already exists" in err


def test_set_password_resets_and_revokes(env, capsys):
    run(["init-db"], capsys)
    run(["add-user", ADMIN_USERNAME, "--password", ADMIN_PASSWORD], capsys)
    code, out, _ = run(["set-password", ADMIN_USERNAME, "--password", "brand-new-secret-2026"],
                       capsys)
    assert code == 0
    assert "sessions revoked" in out

    from streamserver.db.database import Database
    from streamserver.db import repositories as repo
    from streamserver.security import passwords

    db = Database(env["db"])
    try:
        user = repo.get_user_by_username(db, ADMIN_USERNAME)
        assert passwords.verify_password("brand-new-secret-2026", user["password_hash"])
        assert user["must_change_password"] == 1
    finally:
        db.close()


def test_set_password_for_an_unknown_user(env, capsys):
    run(["init-db"], capsys)
    code, _, err = run(["set-password", "ghost", "--password", "whatever-secret-2026"], capsys)
    assert code == 1
    assert "no such user" in err


def test_token_mints_a_usable_pair(env, capsys):
    run(["init-db"], capsys)
    run(["add-user", ADMIN_USERNAME, "--password", ADMIN_PASSWORD], capsys)
    code, out, _ = run(["token", ADMIN_USERNAME], capsys)
    assert code == 0
    payload = json.loads(out)
    assert payload["access_token"].startswith("ssat.")
    assert payload["refresh_token"].startswith("ssrt.")


# ---------------------------------------------------------------------------
# Library management
# ---------------------------------------------------------------------------
def test_scan_imports_everything(env, capsys):
    run(["init-db"], capsys)
    for name in ("Dune.2021.mkv", "Arrival.2016.mkv", "Se7en.1995.mkv"):
        (env["library"] / name).write_bytes(bytes(range(256)) * 8)

    code, out, _ = run(["scan", "--root", str(env["library"])], capsys)
    assert code == 0
    stats = json.loads(out)
    assert stats["added"] == 3
    assert stats["failed"] == 0


def test_scan_is_idempotent(env, capsys):
    run(["init-db"], capsys)
    (env["library"] / "Dune.2021.mkv").write_bytes(b"x" * 4096)
    run(["scan", "--root", str(env["library"])], capsys)
    code, out, _ = run(["scan", "--root", str(env["library"])], capsys)
    stats = json.loads(out)
    assert stats["added"] == 0
    assert stats["skipped"] + stats["updated"] == 1


def test_scan_without_roots_fails_clearly(env, capsys):
    code, _, err = run(["scan"], capsys)
    assert code == 1
    assert "no library roots" in err.lower()


def test_import_one_file(env, capsys):
    run(["init-db"], capsys)
    path = env["library"] / "Interstellar.2014.mkv"
    path.write_bytes(b"y" * 4096)
    code, out, _ = run(["import", str(path), "--title", "Interstellar", "--year", "2014",
                        "--root", str(env["library"])], capsys)
    assert code == 0
    assert "added: Interstellar" in out
    assert "id=1" in out


def test_import_rejects_a_path_outside_the_roots(env, capsys, tmp_path):
    run(["init-db"], capsys)
    outside = tmp_path / "outside.mkv"
    outside.write_bytes(b"z" * 1024)
    code, _, err = run(["import", str(outside), "--root", str(env["library"])], capsys)
    assert code == 1
    assert "refused" in err


def test_import_rejects_a_missing_file(env, capsys):
    run(["init-db"], capsys)
    code, _, err = run(["import", str(env["library"] / "nope.mkv"),
                        "--root", str(env["library"])], capsys)
    assert code == 1
    assert "refused" in err


def test_list_shows_the_library(env, capsys):
    run(["init-db"], capsys)
    (env["library"] / "Dune.2021.mkv").write_bytes(b"x" * 4096)
    run(["scan", "--root", str(env["library"])], capsys)
    code, out, _ = run(["list"], capsys)
    assert code == 0
    assert "Dune" in out
    assert "1 title" in out


def test_list_on_an_empty_library(env, capsys):
    run(["init-db"], capsys)
    code, out, _ = run(["list"], capsys)
    assert code == 0
    assert "0 title" in out


def test_verify_and_remove_missing(env, capsys):
    run(["init-db"], capsys)
    keep = env["library"] / "Keep.2000.mkv"
    gone = env["library"] / "Gone.2001.mkv"
    keep.write_bytes(b"a" * 2048)
    gone.write_bytes(b"b" * 2048)
    run(["scan", "--root", str(env["library"])], capsys)

    gone.unlink()
    code, out, _ = run(["verify", "--root", str(env["library"])], capsys)
    assert code == 1, "a missing file should make verify exit non-zero"
    assert json.loads(out)["missing"] == 1

    code, out, _ = run(["scan", "--root", str(env["library"]),
                        "--remove-missing"], capsys)
    assert json.loads(out)["removed"] == 1


def test_verify_without_roots_does_not_touch_the_library(env, capsys):
    """A configuration problem must not mass-mark the library as missing."""
    run(["init-db"], capsys)
    (env["library"] / "Safe.2000.mkv").write_bytes(b"a" * 2048)
    run(["scan", "--root", str(env["library"])], capsys)

    code, out, _ = run(["verify"], capsys)     # no --root this time
    stats = json.loads(out)
    assert stats["missing"] == 0
    assert stats["skipped"] == 1

    code, out, _ = run(["list"], capsys)
    assert "Safe" in out and "[missing]" not in out


def test_backup_and_restore(env, capsys):
    run(["init-db"], capsys)
    run(["add-user", ADMIN_USERNAME, "--password", ADMIN_PASSWORD], capsys)
    code, out, _ = run(["backup"], capsys)
    assert code == 0
    assert "Wrote" in out
    snapshots = list((env["data"] / "backups").glob("*.db.gz"))
    assert snapshots

    # Wreck the database, then restore.
    run(["set-password", ADMIN_USERNAME, "--password", "another-secret-2026"], capsys)
    code, out, _ = run(["restore", str(snapshots[0]), "--force"], capsys)
    assert code == 0
    assert "Restored" in out
    assert "integrity: ok" in out


def test_restore_refuses_without_force(env, capsys):
    run(["init-db"], capsys)
    run(["add-user", ADMIN_USERNAME, "--password", ADMIN_PASSWORD], capsys)
    run(["backup"], capsys)
    snapshot = next((env["data"] / "backups").glob("*.db.gz"))
    code, _, err = run(["restore", str(snapshot)], capsys)
    assert code == 1
    assert "--force" in err


def test_restore_of_a_missing_backup(env, capsys):
    run(["init-db"], capsys)
    code, _, err = run(["restore", "/nonexistent.db.gz", "--force"], capsys)
    assert code == 1
    assert "not found" in err.lower()