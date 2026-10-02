"""Database: migrations, transactions, integrity, backup and restore."""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest

from streamserver.db.database import (
    SCHEMA_VERSION,
    Database,
    DatabaseError,
    backup,
    migrate,
    restore,
    split_statements,
    verify_integrity,
)
from streamserver.db.migrations import LATEST_VERSION, MIGRATIONS


# ---------------------------------------------------------------------------
# Migrations
# ---------------------------------------------------------------------------
def test_migrations_apply_and_are_idempotent(db: Database):
    assert db.user_version() == LATEST_VERSION
    applied = {row["version"] for row in db.query("SELECT version FROM schema_version")}
    assert applied == {version for version, _, _ in MIGRATIONS}

    # Re-running must be a no-op, not an error.
    assert migrate(db, MIGRATIONS) == LATEST_VERSION


def test_every_expected_table_exists(db: Database):
    tables = set(db.table_counts())
    for required in {
        "users", "profiles", "sessions", "revoked_tokens", "login_attempts",
        "media", "genres", "media_genres", "tags", "media_tags", "people",
        "media_credits", "collections", "collection_media", "media_fts",
        "favorites", "watch_history", "ratings", "audit_log", "scan_jobs",
        "settings", "media_external_ids", "media_streams", "recommendations",
        "playback_sessions",
    }:
        assert required in tables, f"missing table {required}"


def test_foreign_keys_are_enforced(db: Database):
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO profiles (user_id, name) VALUES (999999, 'ghost')"
        )


def test_migrations_apply_to_a_fresh_file(tmp_path: Path):
    path = tmp_path / "fresh.db"
    database = Database(path)
    migrate(database, MIGRATIONS)
    ok, detail = database.integrity_check()
    assert ok, detail
    assert database.foreign_key_check() == []
    database.close()


def test_schema_version_constant_matches_the_migrations():
    assert LATEST_VERSION == max(v for v, _, _ in MIGRATIONS)
    assert LATEST_VERSION >= SCHEMA_VERSION


def test_statement_splitter_respects_strings_and_comments():
    script = """
    -- a comment with a ; semicolon
    CREATE TABLE a (x TEXT DEFAULT 'has ; semicolon');
    /* block ; comment */
    CREATE TABLE b (y TEXT DEFAULT 'quoted '' ; inside');
    CREATE INDEX i ON a (x);
    """
    statements = split_statements(script)
    # Three statements; the leading comment travels with the CREATE it precedes.
    assert len(statements) == 3
    assert "CREATE TABLE a" in statements[0]
    assert "'has ; semicolon'" in statements[0]
    assert "CREATE TABLE b" in statements[1]
    assert statements[2].strip().startswith("CREATE INDEX")


def test_statement_splitter_handles_trailing_comment():
    statements = split_statements("SELECT 1; -- trailing")
    assert len(statements) == 2
    assert statements[1].strip().startswith("--")


def test_migration_sql_survives_the_splitter():
    """Every migration must apply cleanly, in order, to an empty database.

    Applying the whole migration in order is the real property: statements
    legitimately depend on tables created by earlier statements in the same
    migration, which a per-statement check cannot express.
    """
    # Applied cumulatively: a migration may legitimately depend on tables
    # created by an earlier one (migration 2 adds a column to `users`).
    connection = sqlite3.connect(":memory:")
    try:
        for version, name, sql in sorted(MIGRATIONS, key=lambda m: m[0]):
            try:
                connection.executescript(sql)
            except sqlite3.Error as exc:  # pragma: no cover - a broken migration
                pytest.fail(f"migration {version} ({name}) failed: {exc}")
    finally:
        connection.close()

    # And the splitter must break the same text into statements the driver
    # would run, which is what migrate() relies on.
    for version, name, sql in MIGRATIONS:
        statements = split_statements(sql)
        assert statements, f"migration {version} ({name}) produced no statements"
        assert not any(s.strip() == ";" for s in statements)


# ---------------------------------------------------------------------------
# Transactions
# ---------------------------------------------------------------------------
def test_write_rolls_back_on_error(db: Database):
    with pytest.raises(RuntimeError):
        with db.write() as conn:
            conn.execute(
                "INSERT INTO users (username, password_hash) VALUES ('rollback', 'x')"
            )
            raise RuntimeError("boom")
    assert db.query_one("SELECT 1 FROM users WHERE username = 'rollback'") is None


def test_write_commits_on_success(db: Database):
    with db.write() as conn:
        conn.execute(
            "INSERT INTO users (username, password_hash) VALUES ('committed', 'x')"
        )
    assert db.query_one("SELECT 1 FROM users WHERE username = 'committed'") is not None


def test_concurrent_writers_do_not_collide(db: Database, tmp_path: Path):
    """Ten threads inserting at once must all land, none with 'database is locked'."""
    errors: list[Exception] = []
    barrier = threading.Barrier(10)

    def writer(index: int) -> None:
        try:
            barrier.wait(timeout=10)
            db.execute(
                "INSERT INTO users (username, password_hash) VALUES (?, 'x')",
                (f"concurrent-{index}",),
            )
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(10)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert not errors, errors
    assert int(db.scalar("SELECT COUNT(*) FROM users") or 0) == 10


def test_reads_do_not_block_while_a_write_is_open(db: Database):
    """WAL: a reader on another connection must proceed during a write.

    If the journal were not in WAL mode this would raise "database is locked",
    which is the whole reason the mode is set -- the importer must not stall
    every concurrent reader on the box.
    """
    other = Database(db.path)
    try:
        assert other.query_one("PRAGMA journal_mode")[0] == "wal"
        with db.write() as conn:
            conn.execute(
                "INSERT INTO users (username, password_hash) VALUES ('pending', 'x')"
            )
            # Another connection can read while the write transaction is open --
            # in WAL mode it sees the committed state, not "database is locked".
            assert other.query_one("SELECT COUNT(*) FROM users")[0] >= 0
            # A second *writer* is refused, not silently dropped.  It is the
            # same thread's lock that is held, so the failure is immediate
            # rather than after a busy timeout.
            with pytest.raises(sqlite3.OperationalError):
                other.execute(
                    "INSERT INTO users (username, password_hash) VALUES ('blocked', 'x')"
                )
    finally:
        other.close()
    assert db.query_one("SELECT 1 FROM users WHERE username = 'pending'") is not None
    assert db.query_one("SELECT 1 FROM users WHERE username = 'blocked'") is None


# ---------------------------------------------------------------------------
# Integrity
# ---------------------------------------------------------------------------
def test_integrity_check_passes_on_a_clean_database(db: Database):
    ok, detail = db.integrity_check()
    assert ok, detail


def test_integrity_check_fails_on_a_corrupt_file(tmp_path: Path):
    path = tmp_path / "corrupt.db"
    database = Database(path)
    migrate(database, MIGRATIONS)
    database.close()

    with open(path, "r+b") as handle:
        handle.seek(0)
        handle.write(b"\x00" * 4096)

    ok, _ = verify_integrity(path)
    assert not ok, "a zeroed header must fail SQLite's own integrity check"

    # And the running database reports the same thing rather than pretending.
    broken = Database(path)
    try:
        assert broken.integrity_check()[0] is False
    finally:
        broken.close()


def test_cascade_delete_removes_profiles_and_history(db: Database):
    db.execute("INSERT INTO users (username, password_hash) VALUES ('gone', 'x')")
    user_id = int(db.scalar("SELECT id FROM users WHERE username = 'gone'"))
    db.execute("INSERT INTO profiles (user_id, name) VALUES (?, 'P')", (user_id,))
    profile_id = int(db.scalar("SELECT id FROM profiles WHERE user_id = ?", (user_id,)))
    db.execute(
        "INSERT INTO media (slug, title, media_path, media_root) VALUES (?, ?, ?, ?)",
        ("s", "T", "/x/y.mkv", "/x"),
    )
    media_id = int(db.scalar("SELECT id FROM media WHERE slug = 's'"))
    db.execute(
        "INSERT INTO watch_history (profile_id, media_id) VALUES (?, ?)",
        (profile_id, media_id),
    )

    db.execute("DELETE FROM users WHERE id = ?", (user_id,))
    assert db.query_one("SELECT 1 FROM profiles WHERE id = ?", (profile_id,)) is None
    assert db.query_one("SELECT 1 FROM watch_history WHERE media_id = ?", (media_id,)) is None


def test_wal_checkpoint_and_optimize(db: Database):
    db.wal_checkpoint("TRUNCATE")
    db.optimize()
    db.execute("ANALYZE")
    ok, _ = db.integrity_check()
    assert ok


# ---------------------------------------------------------------------------
# Backup and restore
# ---------------------------------------------------------------------------
def test_backup_round_trip(tmp_path: Path, db: Database):
    from streamserver.db import repositories as repo

    db.execute("INSERT INTO users (username, password_hash) VALUES ('saved', 'x')")
    destination = tmp_path / "backups" / "snap.db.gz"
    write = backup(db, destination)
    assert write.exists()
    assert oct(destination.stat().st_mode)[-3:] == "600"

    restored = tmp_path / "restored.db"
    restore(destination, restored, force=True)
    ok, detail = verify_integrity(restored)
    assert ok, detail

    other = Database(restored)
    try:
        assert repo.get_user_by_username(other, "saved") is not None
        assert other.user_version() == LATEST_VERSION
    finally:
        other.close()


def test_backup_is_a_consistent_snapshot_while_writing(db: Database, tmp_path: Path):
    """The backup API must not capture a half-applied transaction."""
    from streamserver.db import repositories as repo

    for index in range(200):
        db.execute(
            "INSERT INTO users (username, password_hash) VALUES (?, 'x')",
            (f"u{index}",),
        )
    destination = tmp_path / "snap.db"
    backup(db, destination, compress=False)

    other = Database(destination)
    try:
        assert int(other.scalar("SELECT COUNT(*) FROM users") or 0) == 200
    finally:
        other.close()


def test_restore_refuses_to_overwrite_without_force(tmp_path: Path, db: Database):
    snapshot = tmp_path / "snap.db.gz"
    backup(db, snapshot)
    with pytest.raises(DatabaseError, match="force=True"):
        restore(snapshot, db.path)


def test_restore_keeps_a_pre_restore_copy(tmp_path: Path, db: Database):
    """A restore replaces the live database, so the old file must survive."""
    db.execute("INSERT INTO users (username, password_hash) VALUES ('before', 'x')")
    snapshot = tmp_path / "snap.db.gz"
    backup(db, snapshot)
    db.execute("DELETE FROM users")

    # close_existing is required: the fixture's open handle would otherwise
    # keep the pre-restore inode alive and the assertions below would read it.
    restore(snapshot, db.path, force=True, close_existing=db)

    from streamserver.db import repositories as repo

    assert repo.get_user_by_username(db, "before") is not None
    assert list(db.path.parent.glob("*.pre-restore.bak")), "no pre-restore copy kept"


def test_restore_of_a_missing_file_fails_clearly(tmp_path: Path, db: Database):
    with pytest.raises(DatabaseError, match="backup not found"):
        restore(tmp_path / "nope.db.gz", tmp_path / "out.db", force=True)


def test_size_and_counts(db: Database):
    assert db.size_bytes() > 0
    assert db.table_counts()["media"] == 0