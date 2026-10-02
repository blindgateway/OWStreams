"""Administration routes.

Everything here requires an administrator account, and every action is written
to the audit trail.  There is no path from an ordinary account to any of it:
the check is a dependency on the router itself, so a route added later cannot
forget it.

Import is intentionally exposed over REST *and* reimplemented by the CLI, which
uses the same :class:`Importer` -- both go through identical validation and
containment, and neither accepts a path outside the configured roots.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Body, HTTPException, Query, Request

from ...db import repositories as repo
from ...security import passwords
from ...security.audit import SecurityEventTypes
from ...security.paths import PathViolation
from ..deps import AdminDep, Ctx, client_ip
from ..schemas import (
    AdminUserCreate,
    AdminUserOut,
    HealthOut,
    ImportRequest,
    ImportResultOut,
    LibraryRootIn,
    MediaOut,
    MediaPatch,
    OkResponse,
)

router = APIRouter(prefix="/admin", tags=["administration"])


# ---------------------------------------------------------------------------
# Accounts
# ---------------------------------------------------------------------------
@router.get("/users", response_model=list[AdminUserOut], summary="List accounts")
async def list_users(context: Ctx, admin: AdminDep) -> list[AdminUserOut]:
    return [AdminUserOut(**row) for row in repo.list_users(context.db, include_inactive=True)]


@router.post("/users", response_model=AdminUserOut, status_code=201,
             summary="Create an account")
async def create_user(payload: AdminUserCreate, context: Ctx, admin: AdminDep,
                      request: Request) -> AdminUserOut:
    from ...services.auth import validate_password_strength

    if repo.get_user_by_username(context.db, payload.username):
        raise HTTPException(status_code=409, detail="That username is already taken.")
    validate_password_strength(payload.password, context.config.auth)
    user = repo.create_user(
        context.db, username=payload.username,
        password_hash=passwords.hash_password(
            payload.password,
            n=context.config.auth.scrypt_n, r=context.config.auth.scrypt_r,
            p=context.config.auth.scrypt_p, maxmem=context.config.auth.scrypt_maxmem,
        ),
        is_admin=payload.is_admin, email=payload.email,
        display_name=payload.display_name or payload.username,
    )
    repo.create_profile(context.db, int(user["id"]), "Main")
    context.audit.record(
        SecurityEventTypes.ADMIN_ACTION, outcome="ok", user_id=admin.user_id,
        username=admin.user["username"], ip_address=client_ip(request, context),
        detail={"action": "user.create", "target_user_id": int(user["id"])},
    )
    return AdminUserOut(**user)


@router.delete("/users/{user_id}", response_model=OkResponse,
               summary="Delete an account")
async def delete_user(user_id: int, context: Ctx, admin: AdminDep,
                      request: Request) -> OkResponse:
    user = repo.get_user(context.db, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="Account not found.")
    if int(user_id) == admin.user_id:
        raise HTTPException(status_code=409,
                            detail="You cannot delete the account you are signed in as.")
    remaining_admins = int(context.db.scalar(
        "SELECT COUNT(*) FROM users WHERE is_admin = 1 AND is_active = 1 AND id != ?",
        (user_id,),
    ) or 0)
    if user["is_admin"] and remaining_admins == 0:
        raise HTTPException(status_code=409,
                            detail="At least one administrator must remain.")
    repo.delete_user(context.db, user_id)
    context.audit.record(
        SecurityEventTypes.ADMIN_ACTION, outcome="ok", user_id=admin.user_id,
        username=admin.user["username"], ip_address=client_ip(request, context),
        detail={"action": "user.delete", "target_username": user["username"]},
    )
    return OkResponse(detail="Account deleted.")


@router.patch("/users/{user_id}", response_model=AdminUserOut,
              summary="Activate, deactivate or promote an account")
async def update_user(user_id: int, context: Ctx, admin: AdminDep,
                      payload: dict[str, Any] = Body(...)) -> AdminUserOut:
    allowed = {"is_active", "is_admin"}
    updates = {k: int(bool(v)) for k, v in payload.items() if k in allowed}
    if not updates:
        raise HTTPException(status_code=400, detail="No supported fields supplied.")
    if int(user_id) == admin.user_id and updates.get("is_admin") == 0:
        raise HTTPException(status_code=409, detail="You cannot remove your own admin rights.")
    if int(user_id) == admin.user_id and updates.get("is_active") == 0:
        raise HTTPException(status_code=409, detail="You cannot deactivate your own account.")

    user = repo.get_user(context.db, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="Account not found.")
    if updates.get("is_active") == 0:
        repo.revoke_all_sessions(context.db, user_id, reason="account_deactivated")
    updated = repo.update_user(context.db, user_id, **updates)
    context.audit.record(
        SecurityEventTypes.ADMIN_ACTION, outcome="ok", user_id=admin.user_id,
        username=admin.user["username"],
        detail={"action": "user.update", "target_username": user["username"],
                "fields": sorted(updates)},
    )
    return AdminUserOut(**updated)


# ---------------------------------------------------------------------------
# Library management
# ---------------------------------------------------------------------------
@router.get("/media", response_model=list[MediaOut],
            summary="All titles including hidden and missing files")
async def list_all_media(context: Ctx, admin: AdminDep,
                         limit: Annotated[int, Query(ge=1, le=200)] = 100,
                         offset: Annotated[int, Query(ge=0)] = 0) -> list[MediaOut]:
    items = repo.list_media(
        context.db,
        repo.MediaFilter(limit=limit, offset=offset, sort="added",
                         direction="desc", available_only=False),
    )
    return [MediaOut(**item) for item in repo.attach_relations(context.db, items)]


@router.patch("/media/{media_id}", response_model=MediaOut,
              summary="Edit a title's metadata")
async def patch_media(media_id: int, payload: MediaPatch, context: Ctx,
                      admin: AdminDep) -> MediaOut:
    if not repo.get_media_full(context.db, media_id):
        raise HTTPException(status_code=404, detail="Media not found.")
    changes = payload.model_dump(exclude_none=True)
    if not changes:
        raise HTTPException(status_code=400, detail="No fields supplied.")
    updated = context.importer.update_metadata(media_id, changes)
    if not updated:
        raise HTTPException(status_code=404, detail="Media not found.")
    return MediaOut(**repo.attach_relations_one(context.db, updated))


@router.delete("/media/{media_id}", response_model=OkResponse,
               summary="Remove a title from the library (leaves the file)")
async def delete_media(media_id: int, context: Ctx, admin: AdminDep,
                       delete_file: bool = False) -> OkResponse:
    media = repo.get_media_full(context.db, media_id)
    if not media:
        raise HTTPException(status_code=404, detail="Media not found.")
    if delete_file:
        # Even for an admin, deleting a file is refused: this endpoint removes
        # library records, not data.  Files are removed by the operator.
        raise HTTPException(
            status_code=400,
            detail="This server never deletes media files. Remove the file from "
                   "the filesystem, then re-run a scan to drop the record.",
        )
    repo.delete_media(context.db, media_id)
    context.audit.record(
        SecurityEventTypes.ADMIN_ACTION, outcome="ok", user_id=admin.user_id,
        username=admin.user["username"],
        detail={"action": "media.delete", "media_id": media_id},
    )
    return OkResponse(detail="Removed from the library.")


@router.get("/library-roots", summary="Configured media roots")
async def library_roots(context: Ctx, admin: AdminDep) -> dict:
    roots = []
    for root in context.registry.list_roots():
        entries = 0
        try:
            entries = sum(1 for _ in root.iterdir())
        except OSError:
            entries = -1
        roots.append({"path": str(root), "exists": root.exists(),
                      "readable": root.is_dir(), "top_level_entries": entries})
    return {"roots": roots, "configured": context.config.media.library_roots}


@router.post("/library-roots", response_model=OkResponse,
             summary="Add a media root (in-memory until it is put in the config file)")
async def add_library_root(payload: LibraryRootIn, context: Ctx, admin: AdminDep,
                           request: Request) -> OkResponse:
    """Validate and add a root.

    The registry is rebuilt from the *configured* roots plus this one, and the
    path is resolved and confirmed to be a readable directory before it is
    accepted -- a typo should fail here, not at the next scan.
    """
    from ...security.paths import MediaRootRegistry

    candidate = Path(payload.path).expanduser()
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise HTTPException(status_code=400,
                            detail=f"Path does not exist or cannot be read: {exc}") from exc
    if not resolved.is_dir():
        raise HTTPException(status_code=400, detail="Path is not a directory.")

    current = list(context.config.media.library_roots)
    if str(resolved) in current:
        return OkResponse(detail="Already configured.")
    current.append(str(resolved))
    context.config.media.library_roots = current
    context.registry.__init__(tuple(Path(p) for p in current))  # rebuild
    context.importer.registry = context.registry
    context.library.config.media.library_roots = current

    context.audit.record(
        SecurityEventTypes.ADMIN_ACTION, outcome="ok", user_id=admin.user_id,
        username=admin.user["username"], ip_address=client_ip(request, context),
        detail={"action": "library.root.add", "path": str(resolved)},
    )
    return OkResponse(
        detail=f"Added {resolved}. Persist it in the server config file to "
               f"survive a restart."
    )


@router.post("/import", response_model=ImportResultOut,
             summary="Import specific files, or rescan the library")
async def run_import(payload: ImportRequest, context: Ctx, admin: AdminDep,
                     request: Request) -> ImportResultOut:
    """Add media.

    ``paths`` are absolute paths on the *server's* filesystem.  They are
    resolved against the configured roots and refused if they escape, so this
    endpoint cannot be used to read or register arbitrary files on the host.
    """
    from ...services.importer import PathNotImportable

    added, updated, errors = 0, 0, []
    for raw in payload.paths[:1000]:
        try:
            result = context.importer.add_path(raw, refresh_metadata=payload.refresh_metadata)
            if result.created:
                added += 1
            else:
                updated += 1
        except PathNotImportable as exc:
            errors.append(str(exc)[:200])
        except PathViolation as exc:
            errors.append(str(exc)[:200])
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{Path(raw).name}: {exc}"[:200])

    stats: Any = None
    if payload.scan:
        stats = context.importer.scan(
            remove_missing=payload.remove_missing,
            reimport_all=payload.reimport_all,
        )
        added += stats.added
        updated += stats.updated
        errors.extend(stats.errors)

    if not payload.paths and not payload.scan:
        raise HTTPException(status_code=400,
                            detail="Supply paths to import, or set scan=true.")

    context.audit.record(
        SecurityEventTypes.IMPORT_RUN, outcome="ok", user_id=admin.user_id,
        username=admin.user["username"], ip_address=client_ip(request, context),
        detail={"paths": len(payload.paths), "scan": payload.scan,
                "added": added, "updated": updated, "errors": len(errors)},
    )
    if stats is not None:
        return ImportResultOut(**stats.as_dict() | {"added": added, "updated": updated})
    return ImportResultOut(added=added, updated=updated, errors=errors)


@router.post("/verify", summary="Re-check every media file against the filesystem")
async def verify(context: Ctx, admin: AdminDep, deep: bool = False) -> dict:
    return context.importer.verify_integrity(deep=deep)


@router.post("/reindex", response_model=OkResponse,
             summary="Rebuild the full-text search index")
async def reindex(context: Ctx, admin: AdminDep) -> OkResponse:
    count = context.importer.reset_search_index()
    return OkResponse(detail=f"Reindexed {count} titles.")


# ---------------------------------------------------------------------------
# Maintenance and observability
# ---------------------------------------------------------------------------
@router.get("/audit", summary="Recent security events")
async def audit_log(context: Ctx, admin: AdminDep,
                    limit: Annotated[int, Query(ge=1, le=500)] = 100,
                    action: str | None = None) -> list[dict]:
    return context.audit.recent(limit=limit, action=action)


@router.get("/settings", summary="Server settings that are safe to display")
async def settings(context: Ctx, admin: AdminDep) -> dict:
    config = context.config
    return {
        "server": {
            "host": config.server.host,
            "port": config.server.port,
            "cors_origins": config.server.cors_origins,
            "trusted_proxies": config.server.trusted_proxies,
        },
        "auth": {
            "access_token_ttl_seconds": config.auth.access_token_ttl_seconds,
            "refresh_token_ttl_seconds": config.auth.refresh_token_ttl_seconds,
            "max_failed_logins": config.auth.max_failed_logins,
            "lockout_seconds": config.auth.lockout_seconds,
            "password_min_length": config.auth.password_min_length,
        },
        "media": {
            "library_roots": config.media.library_roots,
            "ffprobe_path": config.media.ffprobe_path,
            "completion_threshold": config.media.completion_threshold,
        },
        "metadata": {
            "default_provider": config.metadata.default_provider,
            # Never the key itself -- only whether one is configured.
            "tmdb_configured": bool(config.metadata.tmdb_api_key),
            "imdb_configured": bool(config.metadata.imdb_api_key),
        },
        "storage": {
            "data_dir": str(config.storage.data_dir),
            "database_path": str(config.database_file),
            "image_dir": str(config.image_path),
            "log_dir": str(config.log_path),
        },
    }


@router.post("/maintenance/prune", response_model=OkResponse,
             summary="Remove expired tokens, sessions and old login attempts")
async def prune(context: Ctx, admin: AdminDep) -> OkResponse:
    from ...security import tokens as token_module

    tokens_removed = 0
    with context.db.write() as conn:
        cur = conn.execute(
            "DELETE FROM revoked_tokens WHERE expires_at <= datetime('now')"
        )
        tokens_removed = cur.rowcount or 0
    sessions_removed = repo.prune_sessions(context.db)
    attempts_removed = context.auth.lockout.prune()
    playback_removed = repo.prune_playback_sessions(context.db)
    audit_removed = context.audit.prune(context.config.storage.audit_retention_days)
    del token_module
    return OkResponse(detail=(
        f"Pruned {tokens_removed} tokens, {sessions_removed} sessions, "
        f"{attempts_removed} login attempts, {playback_removed} playback sessions "
        f"and {audit_removed} audit records."
    ))


@router.post("/backup", response_model=OkResponse, summary="Write a database backup")
async def make_backup(context: Ctx, admin: AdminDep, request: Request) -> OkResponse:
    from datetime import UTC, datetime

    from ...db.database import backup as make_backup_file

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    destination = context.config.backup_path / f"streamserver-{stamp}.db.gz"
    make_backup_file(context.db, destination)
    context.audit.record(
        SecurityEventTypes.ADMIN_ACTION, outcome="ok", user_id=admin.user_id,
        username=admin.user["username"], ip_address=client_ip(request, context),
        detail={"action": "backup", "file": destination.name},
    )
    return OkResponse(detail=f"Backup written to {destination}.")


@router.post("/maintenance/vacuum", response_model=OkResponse,
             summary="Compact the database file")
async def vacuum(context: Ctx, admin: AdminDep) -> OkResponse:
    context.db.wal_checkpoint("TRUNCATE")
    context.db.vacuum()
    return OkResponse(detail="Database compacted.")


@router.get("/health", response_model=HealthOut, summary="Detailed health report")
async def health(context: Ctx, admin: AdminDep) -> HealthOut:
    import time

    from ... import __version__, API_VERSION
    from ...media.probe import MediaProbe

    stats = repo.library_stats(context.db)
    ok, detail = context.db.integrity_check()
    return HealthOut(
        status="ok" if ok else "degraded",
        version=__version__,
        api_version=API_VERSION,
        database="ok" if ok else f"integrity check failed: {detail[:200]}",
        library_count=int(stats.get("total") or 0),
        missing_files=int(stats.get("missing") or 0),
        uptime_seconds=round(time.time() - context.started_at, 1),
        library_roots=[str(p) for p in context.registry.list_roots()],
        ffprobe_available=MediaProbe(context.config.media.ffprobe_path).available,
    )


__all__ = ["router"]