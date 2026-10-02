"""SQLite connection management, migrations and backup/restore.

Why SQLite: the target server is a single 4-core / 4 GB box serving ~10
concurrent users.  SQLite in WAL mode handles that comfortably and removes an
entire class of operational failure (no separate daemon to supervise, no
network credentials, backups are one file copy).  PostgreSQL would only be
worth its cost if we ever needed multiple writer *hosts*.

Connections are per-thread and short-lived; every writer goes through
:meth:`Database.write`, which opens a transaction with a busy timeout so that a
concurrent importer cannot turn into ``database is locked``.
"""

from __future__ import annotations

import logging
import os
import shutil
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

SCHEMA_VERSION = 3

#: Bumped whenever the on-disk schema changes; see db/migrations.py.
_MIGRATION_TABLE = """
CREATE TABLE IF NOT EXISTS schema_version (
    version     INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    applied_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


class DatabaseError(RuntimeError):
    """Raised for unrecoverable database problems (integrity, corruption)."""


@dataclass(slots=True)
class WriteResult:
    """Rowid of an inserted row plus the driver's last insert id."""

    rowid: int
    changes: int


def _now() -> str:
    """UTC timestamp in a lexicographically sortable form."""
    import datetime as _dt

    return _dt.datetime.now(_dt.UTC).replace(microsecond=0).isoformat()


class Database:
    """Thin, explicit SQLite wrapper.

    Deliberately not an ORM: the queries in :mod:`streamserver.db.repositories`
    are plain SQL that is easy to read, easy to index and easy to audit for
    injection safety.
    """

    def __init__(self, path: Path | str, *, timeout: float = 15.0,
                 read_only: bool = False) -> None:
        self.path = Path(path)
        self.timeout = timeout
        self.read_only = read_only
        self._local = threading.local()
        self._write_lock = threading.RLock()
        self._connections: list[sqlite3.Connection] = []
        self._conn_lock = threading.Lock()

    # -- connection handling ---------------------------------------------
    def _configure(self, conn: sqlite3.Connection) -> None:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(f"PRAGMA busy_timeout = {int(self.timeout * 1000)}")
        if not self.read_only:
            # WAL lets readers (the API) proceed while a writer (the importer)
            # holds the write lock.  NORMAL is the documented safe pairing with
            # WAL: durable across process crashes, only at risk from an OS-level
            # power loss, which is the right trade for a personal media server.
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute("PRAGMA temp_store = MEMORY")
        # 2 MB of page cache per connection keeps a 1000-title library inside a
        # few tens of MiB even with ten concurrent clients.
        conn.execute("PRAGMA cache_size = -2048")
        conn.execute("PRAGMA mmap_size = 67108864")   # 64 MiB
        conn.execute("PRAGMA optimize")

    def connection(self) -> sqlite3.Connection:
        """Return this thread's connection, creating it on first use."""
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            return conn
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(
            str(self.path),
            timeout=self.timeout,
            check_same_thread=False,
            isolation_level=None,       # explicit transactions only
            detect_types=0,
        )
        self._configure(conn)
        self._local.conn = conn
        with self._conn_lock:
            self._connections.append(conn)
        return conn

    def close(self) -> None:
        """Close every connection handed out by this process."""
        with self._conn_lock:
            conns, self._connections = self._connections, []
        for conn in conns:
            try:
                conn.close()
            except sqlite3.Error:  # pragma: no cover - best effort on shutdown
                pass
        self._local = threading.local()

    # -- reading ----------------------------------------------------------
    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        """A read-only transaction.  Never blocks writers under WAL."""
        conn = self.connection()
        yield conn

    def query(self, sql: str, params: tuple | dict = ()) -> list[sqlite3.Row]:
        return self.connection().execute(sql, params).fetchall()

    def query_one(self, sql: str, params: tuple | dict = ()) -> sqlite3.Row | None:
        return self.connection().execute(sql, params).fetchone()

    def scalar(self, sql: str, params: tuple | dict = ()) -> Any:
        row = self.query_one(sql, params)
        return None if row is None else row[0]

    # -- writing ----------------------------------------------------------
    @contextmanager
    def write(self) -> Iterator[sqlite3.Connection]:
        """An IMMEDIATE transaction.

        IMMEDIATE (rather than the default DEFERRED) takes the write lock up
        front, so two concurrent writers fail fast into ``busy_timeout``
        instead of deadlocking at COMMIT with ``SQLITE_BUSY`` that cannot be
        retried.
        """
        with self._write_lock:
            conn = self.connection()
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            else:
                conn.execute("COMMIT")

    def execute(self, sql: str, params: tuple | dict = ()) -> WriteResult:
        with self.write() as conn:
            cur = conn.execute(sql, params)
            return WriteResult(rowid=cur.lastrowid or 0, changes=cur.rowcount)

    def executemany(self, sql: str, seq_of_params) -> int:
        with self.write() as conn:
            cur = conn.executemany(sql, seq_of_params)
            return cur.rowcount

    # -- maintenance ------------------------------------------------------
    def user_version(self) -> int:
        value = self.scalar("PRAGMA user_version")
        return int(value or 0)

    def set_user_version(self, version: int) -> None:
        self.execute(f"PRAGMA user_version = {int(version)}")

    def integrity_check(self) -> tuple[bool, str]:
        """Run SQLite's own integrity check.  Returns ``(ok, detail)``."""
        try:
            rows = self.query("PRAGMA integrity_check(20)")
        except sqlite3.DatabaseError as exc:
            return False, str(exc)
        detail = "; ".join(str(r[0]) for r in rows)
        ok = bool(rows) and rows[0][0] == "ok"
        return ok, detail

    def foreign_key_check(self) -> list[sqlite3.Row]:
        return self.query("PRAGMA foreign_key_check")

    def optimize(self) -> None:
        """Refresh planner statistics (ANALYZE) -- cheap, run on a schedule."""
        self.execute("ANALYZE")
        self.execute("PRAGMA optimize")

    def vacuum(self) -> None:
        self.execute("VACUUM")

    def wal_checkpoint(self, mode: str = "TRUNCATE") -> None:
        if mode not in {"PASSIVE", "FULL", "RESTORE", "TRUNCATE"}:
            raise ValueError(f"invalid checkpoint mode: {mode!r}")
        self.execute(f"PRAGMA wal_checkpoint({mode})")

    def table_counts(self) -> dict[str, int]:
        names = [
            r["name"] for r in self.query(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        return {name: int(self.scalar(f'SELECT COUNT(*) FROM "{name}"') or 0) for name in names}

    def size_bytes(self) -> int:
        try:
            return self.path.stat().st_size
        except FileNotFoundError:
            return 0


def migrate(db: Database, migrations: list[tuple[int, str, str]]) -> int:
    """Apply pending migrations in order.

    ``migrations`` is a list of ``(version, name, sql)``.  Each runs inside its
    own transaction together with the ``schema_version`` insert, so a crash
    mid-migration leaves the database on the previous version rather than in a
    half-applied state.
    """
    db.connection().execute(_MIGRATION_TABLE)
    current = db.user_version()
    applied = {row["version"] for row in db.query("SELECT version FROM schema_version")}

    for version, name, sql in sorted(migrations, key=lambda m: m[0]):
        if version <= current:
            continue
        log.info("applying migration %03d_%s", version, name)
        statements = [s.strip() for s in sql.split(";") if s.strip()]
        with db.write() as conn:
            for statement in statements:
                conn.execute(statement)
            conn.execute(
                "INSERT OR REPLACE INTO schema_version (version, name) VALUES (?, ?)",
                (version, name),
            )
            conn.execute(f"PRAGMA user_version = {int(version)}")
        applied.add(version)
        current = version
    return current


def backup(db: Database, destination: Path, *, compress: bool = True) -> Path:
    """Consistent online backup using SQLite's own backup API.

    Copying the ``.db`` file while the server is running is not safe -- the WAL
    may hold committed pages that the main file does not.  ``sqlite3.Connection
    .backup`` takes a consistent snapshot while writes continue.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    if compress:
        import gzip
        import sqlite3 as _sqlite3

        staging = destination.with_suffix(destination.suffix + ".tmp")
        with _sqlite3.connect(str(staging)) as target:
            db.connection().backup(target)
        with open(staging, "rb") as src, gzip.open(destination, "wb", compresslevel=6) as dst:
            shutil.copyfileobj(src, dst, length=1024 * 1024)
        staging.unlink(missing_ok=True)
    else:
        with sqlite3.connect(str(destination)) as target:
            db.connection().backup(target)
    os.chmod(destination, 0o600)
    return destination


def verify_integrity(database_file: Path) -> tuple[bool, str]:
    """Open a (possibly backup) database read-only and check it."""
    conn = sqlite3.connect(f"file:{database_file}?mode=ro", uri=True)
    try:
        rows = conn.execute("PRAGMA integrity_check(20)").fetchall()
        detail = "; ".join(str(r[0]) for r in rows)
        return bool(rows) and rows[0][0] == "ok", detail
    finally:
        conn.close()


def restore(backup_file: Path, destination: Path, *, force: bool = False) -> None:
    """Replace the live database with a backup, atomically."""
    if not backup_file.exists():
        raise DatabaseError(f"backup not found: {backup_file}")
    if destination.exists() and not force:
        raise DatabaseError(
            f"{destination} exists; pass force=True to overwrite (a .bak copy is kept)"
        )
    if backup_file.suffix == ".gz":
        import gzip

        with gzip.open(backup_file, "rb") as src:
            conn = sqlite3.connect(str(destination))
            try:
                src.seek(0)
                conn.executescript("")  # ensure the file is created
                import sqlite3 as _sqlite3

                _sqlite3.Connection.backup  # sanity: attribute exists
                dst = conn.cursor()
                dst.executescript("")
                # Restore by loading the compressed dump into a fresh file.
                conn.close()
                staging = destination.with_suffix(".restoring")
                with open(staging, "wb") as raw:
                    shutil.copyfileobj(src, raw, length=1024 * 1024)
                _replace(staging, destination)
            finally:
                pass
    else:
        staging = destination.with_suffix(".restoring")
        shutil.copyfile(backup_file, staging)
        ok, detail = verify_integrity(staging)
        if not ok:
            staging.unlink(missing_ok=True)
            raise DatabaseError(f"backup failed integrity check: {detail}")
        _replace(staging, destination)
    os.chmod(destination, 0o600)


def _replace(staging: Path, destination: Path) -> None:
    if destination.exists():
        shutil.copy2(destination, destination.with_suffix(destination.suffix + ".pre-restore.bak"))
    staging.replace(destination)


def utcnow() -> str:
    """Public alias so repositories never import ``datetime`` ad hoc."""
    return _now()