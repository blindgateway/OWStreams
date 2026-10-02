"""Database access layer.

:class:`~streamserver.db.database.Database` owns connections and transactions;
:mod:`~streamserver.db.migrations` owns the schema; :mod:`~streamserver.db.repositories`
contains the only SQL in the project.
"""

from .database import Database, DatabaseError, backup, migrate, restore, verify_integrity
from .migrations import LATEST_VERSION, MIGRATIONS

__all__ = [
    "Database",
    "DatabaseError",
    "backup",
    "migrate",
    "restore",
    "verify_integrity",
    "MIGRATIONS",
    "LATEST_VERSION",
]
