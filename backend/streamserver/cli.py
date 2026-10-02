"""Command-line interface.

One entry point, ``streamserver``, with subcommands.  Everything that touches
the library directly -- import, verify, reindex, backup -- goes through the same
:class:`~streamserver.services.importer.Importer` the API uses, so the CLI has
exactly the same path containment and validation as HTTP.

Examples
--------
::

    streamserver serve                       # run the HTTP server
    streamserver init-db                     # create/migrate the database
    streamserver add-user alice              # create an account
    streamserver set-password alice          # reset without knowing the old one
    streamserver scan --root /mnt/media/movies
    streamserver import /mnt/media/movies/Dune.2021.mkv --title "Dune" --year 2021
    streamserver list                       # show what is in the library
    streamserver verify --deep               # re-probe every file
    streamserver backup                      # write a .db.gz snapshot
    streamserver doctor                      # diagnose a deployment
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import secrets
import sys
from pathlib import Path

from . import API_VERSION, __version__
from .config import Config, ConfigError, dump_config, load_config
from .db.database import Database, backup as write_backup, migrate, verify_integrity
from .db.migrations import LATEST_VERSION, MIGRATIONS
from .logging_setup import setup_logging
from .security import passwords


def _die(message: str, code: int = 1) -> "NoReturn":  # type: ignore[valid-type]
    print(f"error: {message}", file=sys.stderr)
    raise SystemExit(code)


def _open_database(config: Config) -> Database:
    config.ensure_directories()
    db = Database(config.database_file)
    migrate(db, MIGRATIONS)
    return db


def _load(args) -> Config:
    try:
        return load_config(args.config, overrides=_overrides(args))
    except ConfigError as exc:
        _die(str(exc))


def _overrides(args) -> dict:
    overrides: dict[str, object] = {}
    if getattr(args, "data_dir", None):
        overrides["storage.data_dir"] = args.data_dir
    if getattr(args, "host", None):
        overrides["server.host"] = args.host
    if getattr(args, "port", None):
        overrides["server.port"] = args.port
    if getattr(args, "root", None):
        overrides["media.library_roots"] = [str(Path(r).expanduser()) for r in args.root]
    if getattr(args, "log_level", None):
        overrides["storage.log_level"] = args.log_level
    return overrides


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------
def cmd_serve(args) -> int:
    from .api.app import run

    config = _load(args)
    setup_logging(
        config.log_path, level=config.storage.log_level,
        to_file=config.storage.log_to_file,
        retention_days=config.storage.audit_retention_days,
    )
    print(f"Stream server {__version__} (API {API_VERSION})")
    print(f"  data      {config.storage.data_dir}")
    print(f"  database  {config.database_file}")
    print(f"  roots     {', '.join(config.media.library_roots) or '(none configured)'}")
    print(f"  listening http://{config.server.host}:{config.server.port}")
    run(config)
    return 0


def cmd_init_db(args) -> int:
    config = _load(args)
    db = _open_database(config)
    version = db.user_version()
    ok, detail = db.integrity_check()
    db.close()
    print(f"Database ready at {config.database_file}")
    print(f"  schema version {version} (latest supported: {LATEST_VERSION})")
    print(f"  integrity: {'ok' if ok else detail}")
    return 0 if ok else 1


def cmd_init_config(args) -> int:
    config = load_config(args.config) if Path(args.config).exists() else None
    if config is None:
        from .config import Config as _Config

        config = _Config()
    if getattr(args, "root", None):
        config.media.library_roots = [str(Path(r).expanduser().resolve()) for r in args.root]
    if getattr(args, "data_dir", None):
        config.storage.data_dir = Path(args.data_dir).expanduser()
    if getattr(args, "host", None):
        config.server.host = args.host
    if getattr(args, "port", None):
        config.server.port = args.port
    destination = Path(args.config)
    if destination.exists() and not args.force:
        _die(f"{destination} already exists; pass --force to overwrite")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(dump_config(config), encoding="utf-8")
    destination.chmod(0o600)
    print(f"Wrote {destination}")
    return 0


def cmd_add_user(args) -> int:
    config = _load(args)
    db = _open_database(config)
    from .db import repositories as repo

    if repo.get_user_by_username(db, args.username):
        _die(f"user {args.username!r} already exists")

    password = args.password or getpass.getpass("Password: ")
    if not args.password:
        confirmation = getpass.getpass("Confirm: ")
        if password != confirmation:
            _die("passwords do not match")
    from .services.auth import AuthError, validate_password_strength

    try:
        validate_password_strength(password, config.auth)
    except AuthError as exc:
        _die(str(exc))

    user = repo.create_user(
        db, username=args.username,
        password_hash=passwords.hash_password(
            password, n=config.auth.scrypt_n, r=config.auth.scrypt_r,
            p=config.auth.scrypt_p, maxmem=config.auth.scrypt_maxmem,
        ),
        is_admin=args.admin, email=args.email,
        display_name=args.display_name or args.username,
        must_change_password=args.must_change_password,
    )
    repo.create_profile(db, int(user["id"]), args.profile_name or "Main")
    db.close()
    print(f"Created {'administrator ' if args.admin else ''}account {user['username']}")
    return 0


def cmd_set_password(args) -> int:
    config = _load(args)
    db = _open_database(config)
    from .db import repositories as repo
    from .services.auth import AuthError, AuthService, validate_password_strength

    user = repo.get_user_by_username(db, args.username)
    if not user:
        _die(f"no such user: {args.username!r}")
    password = args.password or getpass.getpass("New password: ")
    try:
        validate_password_strength(password, config.auth)
    except AuthError as exc:
        _die(str(exc))

    AuthService(db, config.auth, b"\x00" * 32).reset_password(
        int(user["id"]), password, actor_id=0
    )
    db.close()
    print(f"Password updated for {user['username']}; all sessions revoked.")
    return 0


def cmd_scan(args) -> int:
    config = _load(args)
    setup_logging(config.log_path, level=args.log_level, to_file=False)
    db = _open_database(config)
    from .metadata.providers import ProviderChain, build_provider
    from .security.audit import AuditLogger
    from .security.paths import MediaRootRegistry
    from .services.importer import Importer

    registry = MediaRootRegistry(tuple(Path(p) for p in config.media.library_roots))
    if registry.is_empty:
        _die("no library roots configured (--root or [media] library_roots)")

    chain = ProviderChain([
        build_provider("local"),
        build_provider(
            config.metadata.default_provider,
            api_key=config.metadata.tmdb_api_key or config.metadata.imdb_api_key,
            timeout=config.metadata.request_timeout_seconds,
        ),
    ])
    importer = Importer(db, config, registry, provider=chain,
                        audit=AuditLogger(db, enabled=True))
    stats = importer.scan(
        remove_missing=args.remove_missing, reimport_all=args.reimport_all
    )
    db.close()
    print(json.dumps(stats.as_dict(), indent=2))
    return 0 if stats.failed == 0 else 1


def cmd_import(args) -> int:
    config = _load(args)
    setup_logging(config.log_path, level=args.log_level, to_file=False)
    db = _open_database(config)
    from .security.audit import AuditLogger
    from .security.paths import MediaRootRegistry
    from .services.importer import Importer, PathNotImportable

    registry = MediaRootRegistry(tuple(Path(p) for p in config.media.library_roots))
    importer = Importer(db, config, registry, audit=AuditLogger(db, enabled=True))

    failures = 0
    for raw in args.paths:
        try:
            result = importer.add_path(
                raw, title=args.title, year=args.year,
                refresh_metadata=args.refresh_metadata,
                content_type=args.content_type,
            )
            verb = "added" if result.created else "updated"
            print(f"{verb}: {result.title} (id={result.media_id}, slug={result.slug}, "
                  f"metadata={result.metadata_source})")
        except PathNotImportable as exc:
            failures += 1
            print(f"refused: {raw}: {exc}", file=sys.stderr)
    db.close()
    return 1 if failures else 0


def cmd_list(args) -> int:
    config = _load(args)
    db = _open_database(config)
    from .db import repositories as repo

    total = int(db.scalar("SELECT COUNT(*) FROM media") or 0)
    print(f"{total} title(s) in {config.database_file}\n")
    rows = db.query(
        "SELECT id, slug, title, year, runtime_minutes, file_size_bytes, "
        "integrity_state FROM media ORDER BY sort_title LIMIT ?",
        (args.limit,),
    )
    for row in rows:
        size = row["file_size_bytes"] or 0
        size_mb = f"{size / (1024 * 1024):.0f} MB" if size else "-"
        runtime = f"{row['runtime_minutes']}m" if row["runtime_minutes"] else "-"
        flag = "" if row["integrity_state"] == "ok" else f"  [{row['integrity_state']}]"
        print(f"{row['id']:>5}  {row['title'][:40]:<40} "
              f"{row['year'] or '-':>4}  {runtime:>6}  {size_mb:>9}{flag}")
    db.close()
    return 0


def cmd_verify(args) -> int:
    config = _load(args)
    db = _open_database(config)
    from .security.paths import MediaRootRegistry
    from .services.importer import Importer

    registry = MediaRootRegistry(tuple(Path(p) for p in config.media.library_roots))
    stats = Importer(db, config, registry).verify_integrity(deep=args.deep)
    print(json.dumps(stats, indent=2))
    db.close()
    return 1 if stats["missing"] else 0


def cmd_backup(args) -> int:
    from datetime import UTC, datetime

    config = _load(args)
    db = _open_database(config)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    destination = config.backup_path / f"streamserver-{stamp}.db.gz"
    write_backup(db, destination, compress=not args.no_compress)
    size = destination.stat().st_size
    db.close()
    print(f"Wrote {destination} ({size / 1024:.0f} KiB)")
    return 0


def cmd_restore(args) -> int:
    from .db.database import restore as restore_db

    config = _load(args)
    if Database(config.database_file).connection().execute(
        "SELECT 1 FROM users LIMIT 1"
    ).fetchone():
        if not args.force:
            _die("the database has data in it; pass --force to replace it")
    restore_db(Path(args.backup), config.database_file, force=True)
    ok, detail = verify_integrity(config.database_file)
    print(f"Restored {args.backup} -> {config.database_file}")
    print(f"  integrity: {'ok' if ok else detail}")
    return 0 if ok else 1


def cmd_doctor(args) -> int:
    """Diagnose a deployment.  Exits non-zero when something is wrong."""
    config = _load(args)
    problems: list[str] = []
    notes: list[str] = []

    print(f"Stream server {__version__} (API {API_VERSION})")
    print(f"  python     {sys.version.split()[0]} on {sys.platform}")
    print(f"  data dir   {config.storage.data_dir} "
          f"({'exists' if config.storage.data_dir.exists() else 'MISSING'})")
    if not config.storage.data_dir.exists():
        problems.append("data directory does not exist")

    print(f"  database   {config.database_file}")
    if config.database_file.exists():
        db = Database(config.database_file)
        try:
            version = db.user_version()
            ok, detail = db.integrity_check()
            counts = db.table_counts()
            print(f"    schema    {version} / {LATEST_VERSION}")
            print(f"    integrity {'ok' if ok else 'FAILED: ' + detail}")
            print(f"    titles    {counts.get('media', 0)}")
            print(f"    users     {counts.get('users', 0)}")
            if not ok:
                problems.append("database integrity check failed")
            if version < LATEST_VERSION:
                notes.append(f"schema is at {version}, latest is {LATEST_VERSION}; "
                             f"run 'streamserver init-db'")
        finally:
            db.close()
    else:
        notes.append("database does not exist yet; run 'streamserver init-db'")

    from .media.probe import MediaProbe

    if MediaProbe(config.media.ffprobe_path).available:
        print("  ffprobe    available")
    else:
        notes.append("ffprobe not found: technical metadata will be empty")

    roots = config.media.library_roots
    if not roots:
        problems.append("no library roots configured")
    else:
        print("  library roots:")
        for entry in roots:
            path = Path(entry)
            exists = path.is_dir()
            readable = exists and os.access(path, os.R_OK | os.X_OK)
            print(f"    {'ok ' if readable else 'BAD'} {entry}")
            if not readable:
                problems.append(f"library root is not readable: {entry}")

    try:
        free = os.statvfs(config.storage.data_dir if config.storage.data_dir.exists()
                          else Path("/"))
        free_gib = free.f_bavail * free.f_frsize / (1024 ** 3)
        print(f"  free space {free_gib:.1f} GiB")
        if free_gib < 1:
            problems.append("less than 1 GiB free on the data filesystem")
    except OSError as exc:
        notes.append(f"cannot stat free space: {exc}")

    for note in notes:
        print(f"  note: {note}")
    for problem in problems:
        print(f"  PROBLEM: {problem}", file=sys.stderr)

    return 1 if problems else 0


def cmd_token(args) -> int:
    """Mint a token pair for a user.  For scripting and smoke tests."""
    config = _load(args)
    db = _open_database(config)
    from .db import repositories as repo
    from .security.tokens import create_access_token, create_refresh_token, load_or_create_secret
    from datetime import UTC, datetime, timedelta

    user = repo.get_user_by_username(db, args.username)
    if not user:
        _die(f"no such user: {args.username!r}")
    secret = load_or_create_secret(config.database_file)
    session_id = secrets.token_urlsafe(24)
    plaintext, token_hash = create_refresh_token()
    expires = datetime.now(UTC) + timedelta(days=30)
    repo.create_session(
        db, session_id=session_id, user_id=int(user["id"]),
        refresh_token_hash=token_hash, expires_at=expires.isoformat(),
        device_name="cli", client_version=__version__,
    )
    access, _ = create_access_token(
        secret, user_id=int(user["id"]), session_id=session_id, ttl_seconds=86400
    )
    db.close()
    print(json.dumps({"access_token": access, "refresh_token": plaintext,
                      "expires_in": 86400, "user": user["username"]}, indent=2))
    return 0


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="streamserver",
        description="Personal media library and playback server.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--version", action="version", version=f"streamserver {__version__}")
    parser.add_argument("-c", "--config", default=os.environ.get("STREAMSERVER_CONFIG"),
                        help="path to server.toml")
    parser.add_argument("--log-level", choices=["DEBUG", "INFO", "WARNING", "ERROR"],
                        default=None)

    # Registered on every subparser as well as the top level: argparse's
    # subparser action does not accept the parent's optionals *after* the
    # subcommand, and "streamserver scan --root X" is the natural spelling.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--data-dir", help="override the data directory")
    common.add_argument("--root", action="append",
                        help="add a media root (repeatable)")

    subparsers = parser.add_subparsers(dest="command", required=True)

    def sub(name: str, help: str) -> argparse.ArgumentParser:
        return subparsers.add_parser(name, help=help, parents=[common])

    serve = sub("serve", "run the HTTP server")
    serve.add_argument("--host")
    serve.add_argument("--port", type=int)
    serve.set_defaults(func=cmd_serve)

    init_db = sub("init-db", "create or migrate the database")
    init_db.set_defaults(func=cmd_init_db)

    init_config = sub("init-config", "write a config file")
    init_config.add_argument("--host")
    init_config.add_argument("--port", type=int)
    init_config.add_argument("--force", action="store_true")
    init_config.set_defaults(func=cmd_init_config)

    add_user = sub("add-user", "create an account")
    add_user.add_argument("username")
    add_user.add_argument("--password", help="omit to be prompted")
    add_user.add_argument("--admin", action="store_true")
    add_user.add_argument("--email")
    add_user.add_argument("--display-name")
    add_user.add_argument("--profile-name", default="Main")
    add_user.add_argument("--must-change-password", action="store_true")
    add_user.set_defaults(func=cmd_add_user)

    set_password = sub("set-password", help="reset a password")
    set_password.add_argument("username")
    set_password.add_argument("--password")
    set_password.set_defaults(func=cmd_set_password)

    scan = sub("scan", help="scan the library and import new files")
    scan.add_argument("--remove-missing", action="store_true",
                      help="drop records whose files have disappeared")
    scan.add_argument("--reimport-all", action="store_true",
                      help="re-probe and re-fetch metadata for everything")
    scan.set_defaults(func=cmd_scan)

    imp = sub("import", help="import specific files")
    imp.add_argument("paths", nargs="+")
    imp.add_argument("--title")
    imp.add_argument("--year", type=int)
    imp.add_argument("--content-type", choices=["movie", "series", "episode"])
    imp.add_argument("--refresh-metadata", action="store_true")
    imp.set_defaults(func=cmd_import)

    listing = sub("list", help="show the library")
    listing.add_argument("--limit", type=int, default=50)
    listing.set_defaults(func=cmd_list)

    verify = sub("verify", help="check files against the library")
    verify.add_argument("--deep", action="store_true", help="re-probe every file (slow)")
    verify.set_defaults(func=cmd_verify)

    backup = sub("backup", help="write a database snapshot")
    backup.add_argument("--no-compress", action="store_true")
    backup.set_defaults(func=cmd_backup)

    restore = sub("restore", help="replace the database from a backup")
    restore.add_argument("backup")
    restore.add_argument("--force", action="store_true")
    restore.set_defaults(func=cmd_restore)

    doctor = sub("doctor", help="diagnose the deployment")
    doctor.set_defaults(func=cmd_doctor)

    token = sub("token", help="mint a token pair for scripting")
    token.add_argument("username")
    token.set_defaults(func=cmd_token)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())