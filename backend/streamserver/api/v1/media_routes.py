"""Media playback, progress and artwork routes.

Playback is the part of the server most exposed to abuse, so the rules are
tight and enforced here rather than trusted to the client:

* A **short-lived playback token** (10 minutes, bound to one media row, one
  profile and the ``media:stream`` scope) is required *in addition to* the
  session access token.  A leaked access token therefore cannot stream.
* The stream URL carries **no path**.  The server looks the id up and re-checks
  containment against the library roots immediately before opening the file.
* Every stream request updates the playback session row, which is what lets a
  client resume after a network blip and gives the audit trail a record of what
  was actually played.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated

# NOTE: these ``Annotated`` aliases must be imported *by value*.  A string
# annotation (``caller: "StreamCallerDep"`` under ``from __future__ import
# annotations``) is never resolved by FastAPI, so the dependency is never wired
# and FastAPI instead looks for a query parameter called ``caller``.
from ..deps import (
    CallerDep,
    Ctx,
    ProfileDep,
    StreamProfileDep,
    UncappedCallerDep,
    UncappedStreamCallerDep,
    _enforce_limit,
)

from fastapi import APIRouter, HTTPException, Query, Request, Response, status
from fastapi.responses import StreamingResponse

from ...db import repositories as repo
from ...media import streaming
from ...security import audit, tokens
from ...security.audit import SecurityEventTypes
from ...security.paths import PathViolation
from ..schemas import (
    MediaPathId,
    MediaOut,
    OkResponse,
    PlaybackTokenOut,
    ProgressOut,
    ProgressUpdate,
)

router = APIRouter(prefix="/media", tags=["playback"])

PLAYBACK_TOKEN_TTL = 600          # 10 minutes
PLAYBACK_SESSION_TTL = 6 * 3600   # 6 hours


@router.post("/{media_id}/playback", response_model=PlaybackTokenOut,
             summary="Mint a short-lived token for streaming one title")
async def start_playback(media_id: MediaPathId, request: Request, context: Ctx,
                         caller: UncappedCallerDep, profile_id: ProfileDep,
                         start_offset: Annotated[float, Query(ge=0)] = 0.0,
                         ) -> PlaybackTokenOut:
    media = repo.get_media_full(context.db, media_id)
    if not media or media.get("hidden"):
        raise HTTPException(status_code=404, detail="Media not found.")

    if media.get("integrity_state") == "missing":
        context.audit.record(
            SecurityEventTypes.MEDIA_MISSING, outcome="denied",
            user_id=caller.user_id, profile_id=profile_id, media_id=media_id,
            ip_address=caller.ip,
        )
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail="This title's file is missing from the server.",
        )

    # Resume where the viewer left off, unless the client asked otherwise.
    if start_offset <= 0:
        progress = repo.get_progress(context.db, profile_id, media_id)
        if progress and not progress["completed"]:
            start_offset = float(progress["position_seconds"])

    token, claims = tokens.create_playback_token(
        context.secret,
        user_id=caller.user_id, profile_id=profile_id, media_id=media_id,
        ttl_seconds=PLAYBACK_TOKEN_TTL,
    )
    session_id = secrets.token_urlsafe(18)
    expires = datetime.now(UTC) + timedelta(seconds=PLAYBACK_SESSION_TTL)
    repo.create_playback_session(
        context.db,
        session_id=session_id, profile_id=profile_id, media_id=media_id,
        user_id=caller.user_id, token_hash=hashlib.sha256(token.encode()).hexdigest(),
        expires_at=expires.isoformat(), start_offset=start_offset,
        client_ip=caller.ip,
    )
    context.history.mark_started(profile_id, media_id, start_offset)

    # Every *other* address this title has an open playback session from, plus
    # this one.  A profile legitimately plays on a phone and a television and
    # both may be open at once; two open sessions from two countries is the
    # thing an audit trail is for, and it was not visible anywhere -- the
    # `client_ip` on a session row records only who started it, never that
    # somebody else joined.
    #
    # `MAX_DETAIL_DEVICES` addresses, oldest first, and no claim about the rest:
    # a capped list must say it is capped rather than look exhaustive.
    devices = _known_devices(context, profile_id, media_id)
    device_count = repo.count_playback_addresses(context.db, profile_id, media_id)
    more_devices = device_count >= audit.MAX_DETAIL_DEVICES

    context.audit.record(
        SecurityEventTypes.MEDIA_STREAM_START, outcome="ok",
        user_id=caller.user_id, profile_id=profile_id, media_id=media_id,
        ip_address=caller.ip,
        device_name=_device_name(context, caller),
        detail={
            "start_offset": round(start_offset, 1),
            # `address` as well as `ip_address`: `_with_device_history` folds it
            # into `devices`, which is what keeps the list complete across the
            # events that do not carry one explicitly.
            "address": caller.ip,
            "devices": devices,
            "more_devices": more_devices,
            "device_count": device_count,
        },
    )
    return PlaybackTokenOut(
        playback_token=token,
        expires_in=PLAYBACK_TOKEN_TTL,
        stream_url=f"/api/v1/stream/{media_id}",
        session_id=session_id,
        start_offset=start_offset,
    )


@router.post("/{media_id}/playback/finish", response_model=OkResponse,
             summary="End a playback session and mark the title watched")
async def finish_playback(media_id: MediaPathId, request: Request, context: Ctx,
                          caller: UncappedCallerDep, profile_id: ProfileDep,
                          position_seconds: Annotated[float, Query(ge=0)] = 0.0,
                          session_id: str | None = None) -> OkResponse:
    if session_id:
        # Scoped to this caller: an id supplied by a client is not proof of
        # ownership, and ending somebody else's playback is a cross-account
        # write even though the history it records is correctly attributed.
        closed = repo.end_playback_session(
            context.db, session_id, profile_id=profile_id, user_id=caller.user_id
        )
        if not closed:
            raise HTTPException(status_code=404, detail="No such playback session.")
    context.history.record_progress(
        profile_id, media_id, position_seconds=position_seconds, completed=True
    )
    return OkResponse(detail="Playback recorded.")


@router.get("/{media_id}/progress", response_model=ProgressOut,
            summary="Playback position for one title")
async def get_progress(media_id: MediaPathId, context: Ctx, profile_id: ProfileDep) -> ProgressOut:
    row = context.history.get_progress(profile_id, media_id)
    if not row:
        return ProgressOut(profile_id=profile_id, media_id=media_id,
                           position_seconds=0.0, duration_seconds=0.0,
                           percent=0.0, completed=False)
    return ProgressOut(
        profile_id=profile_id, media_id=media_id,
        position_seconds=float(row["position_seconds"]),
        duration_seconds=float(row["duration_seconds"]),
        percent=float(row["percent"]), completed=bool(row["completed"]),
    )


@router.put("/{media_id}/progress", response_model=ProgressOut,
            summary="Report playback progress (sent periodically by the client)")
async def put_progress(media_id: MediaPathId, payload: ProgressUpdate, context: Ctx,
                       profile_id: ProfileDep, caller: UncappedCallerDep) -> ProgressOut:
    result = context.history.record_progress(
        profile_id, media_id,
        position_seconds=payload.position_seconds,
        duration_seconds=payload.duration_seconds,
        completed=payload.completed,
    )
    return ProgressOut(**result.as_dict())


@router.get("/{media_id}/poster", summary="Poster artwork",
            responses={200: {"content": {"image/jpeg": {}}}, 404: {"description": "No poster"}})
async def poster(media_id: MediaPathId, context: Ctx, caller: CallerDep) -> Response:
    return _serve_artwork(media_id, "poster_key", context)


@router.get("/{media_id}/backdrop", summary="Backdrop artwork",
            responses={200: {"content": {"image/jpeg": {}}}, 404: {"description": "No backdrop"}})
async def backdrop(media_id: MediaPathId, context: Ctx, caller: CallerDep) -> Response:
    return _serve_artwork(media_id, "backdrop_key", context)


def _serve_artwork(media_id: int, column: str, context) -> Response:
    """Serve a stored image.

    Artwork lives under ``image_dir`` keyed by a relative path, never a client
    supplied one.  Even so the resolved path is re-checked against the image
    directory, because a key that came from a metadata file could contain
    traversal.
    """
    row = context.db.query_one(f"SELECT {column} AS key FROM media WHERE id = ?", (media_id,))
    key = row["key"] if row else None
    if not key:
        return Response(status_code=404, headers={"Cache-Control": "public, max-age=300"})

    base = context.config.image_path.resolve(strict=False)
    candidate = (base / key).resolve(strict=False)
    try:
        candidate.relative_to(base)
    except ValueError:
        # A key that escapes the image directory is treated as absent rather
        # than surfaced as an error the client could probe with.
        return Response(status_code=404)
    if not candidate.is_file():
        return Response(status_code=404)

    content_type = {
        ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
        ".webp": "image/webp", ".avif": "image/avif", ".gif": "image/gif",
    }.get(candidate.suffix.lower(), "application/octet-stream")

    data = candidate.read_bytes()
    return Response(
        content=data,
        media_type=content_type,
        headers={
            "Cache-Control": "private, max-age=86400",
            "X-Content-Type-Options": "nosniff",
            "Content-Length": str(len(data)),
        },
    )


__all__ = ["router"]


# ---------------------------------------------------------------------------
# Streaming lives on its own router so it can be excluded from any future
# "no media" deployment mode and so its security properties are in one file.
# ---------------------------------------------------------------------------
stream_router = APIRouter(prefix="/stream", tags=["playback"])


@stream_router.get("/{media_id}", summary="Stream a title (Range requests supported)")
async def stream(
    media_id: int,
    request: Request,
    context: Ctx,
    profile_id: StreamProfileDep,
    caller: UncappedStreamCallerDep,
) -> Response:
    """Serve media bytes.

    Authorisation: an access *or* playback token in ``Authorization``, plus the
    profile it belongs to.  A playback token additionally has to name this
    title (and this profile), which is enforced below.

    The media row is resolved server-side and the resulting path is
    re-validated against the library roots immediately before the file is
    opened, so a symlink swapped in after import cannot widen access.
    """

    _enforce_limit(context, "stream", f"profile:{profile_id}", request=request)

    # Read the session before anything can fail, so that a request this session is
    # not entitled to still records which address asked.  A capture attempt is
    # evidence precisely when it was refused, so the refusal -- not only the
    # success -- is what names the address.
    try:
        session = repo.get_playback_session(
            context.db, _session_for(context, caller, profile_id, media_id)
        )
    except Exception:  # noqa: BLE001 - auditing must never break a request
        session = None

    # The addresses already on this profile's record for this title, which is
    # what makes a *second* address visible on the first stream rather than
    # only on the next playback start.  The start event carries the same list;
    # this is the one that runs mid-film.
    #
    # `answer` comes first because the session is the evidence: on a seek from
    # a second address the proxy's address is the same as the session's, and a
    # later lookup would compare them and find them equal -- the signature of a
    # load balancer, and the one thing that distinguishes it from a second
    # device on the same network.  That address goes on the playback-session row
    # so it is visible to the *next* request, which is where the comparison
    # belongs.
    answer = caller.ip
    session = _remember_answer(context, profile_id, media_id, session, answer)

    try:
        known_devices = _known_devices(context, profile_id, media_id)
    except Exception:  # noqa: BLE001 - auditing must never break a request
        known_devices = [answer] if answer else []

    media = repo.get_media_full(context.db, media_id)
    if not media or media.get("hidden"):
        _record_denial(context, caller, profile_id, media_id, session,
                       "media_not_found")
        raise HTTPException(status_code=404, detail="Media not found.")

    # Already known to be gone: answer from the recorded state rather than
    # opening the file again for every Range request of a missing title.
    if media.get("integrity_state") == "missing":
        _record_denial(context, caller, profile_id, media_id, session,
                       "recorded_missing")
        raise HTTPException(status_code=410, detail="This title's file is missing.")

    # A media-scoped token is bound to one title; refuse it for another.
    if caller.claims.typ == tokens.TOKEN_TYPE_MEDIA:
        if caller.claims.media_id != media_id:
            _record_denial(context, caller, profile_id, media_id, session,
                           "playback_token_bound_to_other_media",
                           extra={"minted_for_media_id": caller.claims.media_id})
            raise HTTPException(status_code=403, detail="This playback token is for another title.")
        if caller.claims.profile_id not in (None, profile_id):
            _record_denial(context, caller, profile_id, media_id, session,
                           "playback_token_bound_to_other_profile")
            raise HTTPException(status_code=403, detail="This playback token is for another profile.")

    # This request is about to be served.  Two things are worth a row: an
    # address that is *not* already on the record -- somebody else watching a
    # film that is already playing elsewhere -- and nothing else.  A thousand
    # Range requests from the address that started it record nothing, because
    # they are the playback working.
    _record_served(context, caller, profile_id, media_id, session,
                     known_devices, answer)

    try:
        path, size = streaming.resolve_stream_target(
            Path(media["media_path"]), context.registry
        )
    except PathViolation as exc:
        repo.set_integrity_state(context.db, media_id, "missing")
        _record_denial(context, caller, profile_id, media_id, session,
                       "path_violation", extra={"detail": str(exc)[:120]})
        raise HTTPException(status_code=410, detail="This title's file is missing.") from exc

    try:
        rng = streaming.parse_range_header(request.headers.get("range"), size)
    except (streaming.RangeNotSatisfiable, streaming.RangeNotParseable) as exc:
        _record_denial(context, caller, profile_id, media_id, session,
                       "unsatisfiable_range")
        return Response(
            status_code=416,
            headers={"Content-Range": f"bytes */{size}",
                     "Accept-Ranges": "bytes"},
        )

    if rng is not None:
        start, end = streaming.clamp_span(
            rng.start, rng.end, size, context.config.media.max_range_span
        )
        rng = streaming.ResolvedRange(start=start, end=end, total_size=size)

    is_partial = rng is not None
    start = rng.start if rng else 0
    length = rng.length if rng else size
    headers = streaming.build_headers(path=path, rng=rng or
                                      streaming.ResolvedRange(0, max(size - 1, 0), size),
                                      is_partial=is_partial)

    async def body():
        try:
            async for chunk in streaming.iter_file_chunks(
                path, start, length,
                chunk_size=context.config.media.stream_chunk_size,
            ):
                yield chunk
        finally:
            # Recorded even if the client disconnects mid-transfer: a partial
            # watch still counts as a watch.
            try:
                repo.touch_playback_session(context.db, _session_for(context, caller, profile_id,
                                                                     media_id),
                                            bytes_served=length)
            except Exception:  # noqa: BLE001 - never break the response
                pass

    return StreamingResponse(
        body(), status_code=status.HTTP_206_PARTIAL_CONTENT if is_partial
        else status.HTTP_200_OK,
        headers=headers, media_type=headers.get("Content-Type"),
    )


def _device_name(context, caller) -> str | None:
    """What this sign-in called itself, or `None` if it named nothing.

    Read from the login session rather than from a claim: a playback token is
    minted per title and carries no device claim, and the *access* token's
    `session_id` is the only handle that reaches the row the device name lives
    in.  Recorded on the audit rows that name an address, so "this film was
    streamed from two addresses" can be answered with "and these are the
    devices" rather than only with two IP addresses.

    Never raises.  A missing device name is not worth failing a stream over,
    and an audit row with `device_name = NULL` is a complete row.
    """
    if caller.claims.session_id is None:
        return None
    try:
        row = context.db.query_one(
            "SELECT device_name FROM sessions WHERE id = ?",
            (caller.claims.session_id,),
        )
    except Exception:  # noqa: BLE001 - auditing must never break a request
        return None
    return (row["device_name"] if row else None) or None


def _session_for(context, caller, profile_id: int, media_id: int) -> str | None:
    row = context.db.query_one(
        "SELECT id FROM playback_sessions WHERE media_id = ? AND profile_id = ? "
        "AND ended = 0 ORDER BY started_at DESC LIMIT 1",
        (media_id, profile_id),
    )
    return row["id"] if row else None


def _remember_answer(context, profile_id: int, media_id: int,
                     session: dict | None, answer: str | None) -> dict | None:
    """Record `answer` as an address this playback session has streamed from.

    Read the session, note the address, read it again -- in that order, and
    before anything can fail.

    The order is the whole feature.  On a seek from a second device behind a
    proxy, the proxy's address is the same as the one the session started from,
    so a lookup that ran *after* the note would compare them, find them equal,
    and see nothing: which is exactly what the first run of this did, and why
    a second device streaming the same title as a first wrote no audit row at
    all.  Read, write, read.  The first read is the "before".
    """
    if not session or not answer:
        return session
    try:
        if repo.note_playback_address(context.db, session["id"], answer):
            return repo.get_playback_session(context.db, session["id"]) or session
        return session
    except Exception:  # noqa: BLE001 - auditing must never break a request
        return session


def _known_devices(context, profile_id: int, media_id: int) -> list[str]:
    """Every address this profile has an open playback session for, newest first.

    The addresses are the sessions' `client_ip`, which `_remember_answer` keeps
    current: each session names the address it was last streamed from, so a
    profile playing on two devices shows both without either replacing the
    other.  Capped at `MAX_DETAIL_DEVICES`; the caller carries the real count in
    `device_count` so a truncated list can say so.
    """
    return repo.playback_addresses(context.db, profile_id, media_id)[:audit.MAX_DETAIL_DEVICES]


def _record_denial(context, caller, profile_id: int, media_id: int,
                   session: dict | None, reason: str, *,
                   extra: dict | None = None) -> None:
    """One audit row for a stream request that was refused.

    Every refusal, including a 404 for a title that does not exist: a client
    walking the id space *is* the request, and the row is what names the
    address that did it.  `detail` carries no body and no title -- the address,
    the reason and the profile's own session id are the whole of it.
    """
    context.audit.record(
        SecurityEventTypes.MEDIA_STREAM_DENIED, outcome="denied",
        user_id=caller.user_id, profile_id=profile_id, media_id=media_id,
        ip_address=caller.ip,
        device_name=_device_name(context, caller),
        detail={
            "reason": reason,
            "session_id": (session or {}).get("id"),
            "address": caller.ip,
            # `typ` rather than `token_type`: the scrubber's key pattern matches
            # "token" in any case, and a redacted type is worse than none.
            "typ": caller.claims.typ,
            **(extra or {}),
        },
    )


def _record_served(context, caller, profile_id: int, media_id: int,
                   session: dict | None, known_devices: list[str],
                   answer: str | None) -> None:
    """One audit row for a stream request that involves more than one device.

    The single case worth a row mid-film: this profile is playing a title from
    more than one address.  A request from the only address on the record writes
    nothing -- a film is thousands of Range requests from one device, and a row
    each would bury every other event in the table.

    The condition is `len(known_devices) > 1`, and *only* that.  An earlier
    version compared the request's address against the list, and then against
    the session's own address; both are wrong for the same reason, that
    `_remember_answer` has already put this request's address into both by the
    time this runs, so a seek from the second device looked like the first.
    "How many devices is this title playing on" does not have that problem: it
    is answered by the session rows, which are one per device and are not
    rewritten by the request being recorded.

    The recorded action is ``MEDIA_STREAM_DENIED`` with ``outcome="ok"``, which
    reads oddly and is deliberate: it is the one action the audit vocabulary has
    for "somebody streamed a title", and a ``detail.reason`` saying so is worth
    more than a new action name that a `LIKE 'media.stream%'` query would also
    have to learn.
    """
    if len(known_devices) < 2:
        return

    # Re-count rather than infer from the length of the returned list: a
    # truncated list is exactly as long as a complete one, so `len() >= cap`
    # said "there may be more" about eight devices and said nothing about the
    # ninth -- the one case the flag exists for.
    total = repo.count_playback_addresses(context.db, profile_id, media_id)

    context.audit.record(
        SecurityEventTypes.MEDIA_STREAM_DENIED, outcome="ok",
        user_id=caller.user_id, profile_id=profile_id, media_id=media_id,
        ip_address=answer,
        device_name=_device_name(context, caller),
        detail={
            "reason": "second_address_streaming",
            "session_id": (session or {}).get("id"),
            "address": answer,
            "devices": known_devices,
            # The list is capped; say so rather than let eight addresses read
            # as the whole story.
            "more_devices": total >= audit.MAX_DETAIL_DEVICES,
            "device_count": total,
            "typ": caller.claims.typ,
        },
    )