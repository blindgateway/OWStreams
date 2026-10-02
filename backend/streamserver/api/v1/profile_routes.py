"""Profile routes.

Profiles are per-account.  Every route takes the authenticated caller and
validates ownership through :class:`ProfileService`, so a guessed
``profile_id`` in another account's namespace is a 404, not a 403 -- the
difference matters because a 403 confirms the id exists.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from ...services.profiles import (
    PinIncorrectError,
    PinRequiredError,
    ProfileError,
    ProfileService,
)
from ..deps import CallerDep, Ctx, UncappedCallerDep
from ..schemas import (
    OkResponse,
    ProfilePathId,
    ProfileCreate,
    ProfileOut,
    ProfilePinChange,
    ProfilePinSet,
    ProfilePinVerify,
    ProfileReorder,
    ProfileUpdate,
    ProfileSelection,
)

router = APIRouter(prefix="/profiles", tags=["profiles"])


def _handle(exc: ProfileError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail={
        "error": exc.code, "message": str(exc),
    })


@router.get("", response_model=list[ProfileOut], summary="List this account's profiles")
async def list_profiles(caller: CallerDep, context: Ctx) -> list[ProfileOut]:
    profiles = context.profiles.list_for_user(caller.user_id)
    return [ProfileOut(**p.as_dict()) for p in profiles]


@router.post("", response_model=ProfileOut, status_code=201,
             summary="Create a profile")
async def create_profile(payload: ProfileCreate, caller: UncappedCallerDep,
                         context: Ctx) -> ProfileOut:
    try:
        profile = context.profiles.create(
            caller.user_id, payload.name,
            avatar_id=payload.avatar_id, pin=payload.pin,
            is_kids=payload.is_kids, max_watch_minutes=payload.max_watch_minutes,
        )
    except ProfileError as exc:
        raise _handle(exc) from exc
    return ProfileOut(**profile.as_dict())


@router.get("/{profile_id}", response_model=ProfileOut, summary="One profile")
async def get_profile(profile_id: ProfilePathId, caller: CallerDep, context: Ctx) -> ProfileOut:
    try:
        context.profiles.get_for_user(caller.user_id, profile_id)
    except ProfileError as exc:
        raise _handle(exc) from exc
    for profile in context.profiles.list_for_user(caller.user_id):
        if profile.id == profile_id:
            return ProfileOut(**profile.as_dict())
    raise HTTPException(status_code=404, detail="Profile not found.")


@router.patch("/{profile_id}", response_model=ProfileOut,
              summary="Rename a profile or change its settings")
async def update_profile(profile_id: ProfilePathId, payload: ProfileUpdate,
                          caller: UncappedCallerDep,
                          context: Ctx) -> ProfileOut:
    try:
        profile = context.profiles.update(
            caller.user_id, profile_id,
            name=payload.name, avatar_id=payload.avatar_id,
            is_kids=payload.is_kids, max_watch_minutes=payload.max_watch_minutes,
            sort_order=payload.sort_order,
        )
    except ProfileError as exc:
        raise _handle(exc) from exc
    return ProfileOut(**profile.as_dict())


@router.delete("/{profile_id}", response_model=OkResponse, summary="Delete a profile")
async def delete_profile(profile_id: ProfilePathId, caller: UncappedCallerDep,
                          context: Ctx) -> OkResponse:
    try:
        context.profiles.delete(caller.user_id, profile_id)
    except ProfileError as exc:
        raise _handle(exc) from exc
    return OkResponse(detail="Profile deleted.")


@router.post("/reorder", response_model=list[ProfileOut],
             summary="Reorder profiles on the selection screen")
async def reorder(payload: ProfileReorder, caller: UncappedCallerDep,
                   context: Ctx) -> list[ProfileOut]:
    try:
        profiles = context.profiles.reorder(caller.user_id, payload.profile_ids)
    except ProfileError as exc:
        raise _handle(exc) from exc
    return [ProfileOut(**p.as_dict()) for p in profiles]


# ---------------------------------------------------------------------------
# PIN lifecycle
# ---------------------------------------------------------------------------
@router.post("/{profile_id}/pin", response_model=ProfileOut,
             summary="Set or clear a profile PIN")
async def set_pin(profile_id: ProfilePathId, payload: ProfilePinSet,
                  caller: UncappedCallerDep, context: Ctx) -> ProfileOut:
    try:
        profile = context.profiles.set_pin(
            caller.user_id, profile_id, payload.pin, current_pin=payload.current_pin
        )
    except ProfileError as exc:
        raise _handle(exc) from exc
    return ProfileOut(**profile.as_dict())


@router.put("/{profile_id}/pin", response_model=ProfileOut,
            summary="Change an existing profile PIN")
async def change_pin(profile_id: ProfilePathId, payload: ProfilePinChange,
                     caller: UncappedCallerDep, context: Ctx) -> ProfileOut:
    try:
        profile = context.profiles.change_pin(
            caller.user_id, profile_id,
            current_pin=payload.current_pin, new_pin=payload.new_pin,
        )
    except ProfileError as exc:
        raise _handle(exc) from exc
    return ProfileOut(**profile.as_dict())


@router.post("/{profile_id}/unlock", response_model=OkResponse,
             summary="Unlock a PIN-locked profile")
async def unlock(profile_id: ProfilePathId, request: Request,
                 payload: ProfilePinVerify, caller: UncappedCallerDep,
                 context: Ctx) -> OkResponse:
    """Verify a PIN.

    The PIN protects a *local* profile selection; it is not an authentication
    credential and grants nothing beyond this profile.

    The throttle is a server-side token bucket keyed on (user, profile).  The
    ``X-Pin-Attempt`` header is a *display hint* the client sends so it can show
    a countdown without asking; it is never what decides whether a guess is
    allowed, because a client that omits it would otherwise be unthrottled.
    """
    hinted = int(request.headers.get("X-Pin-Attempt", "0") or 0)
    try:
        context.profiles.verify_pin(caller.user_id, profile_id, payload.pin)
    except PinIncorrectError as exc:
        context.audit.record(
            "profile.pin.failed", outcome="denied",
            user_id=caller.user_id, profile_id=profile_id,
            ip_address=caller.ip,
            detail={"client_attempt_hint": min(hinted, 20),
                    "attempts_left": exc.attempts_left},
        )
        raise _handle(exc) from exc
    except PinRequiredError as exc:  # pragma: no cover - defensive
        raise _handle(exc) from exc
    except ProfileError as exc:
        raise _handle(exc) from exc
    return OkResponse(detail="Profile unlocked.")


@router.get("/{profile_id}/locked", summary="Whether a profile needs a PIN")
async def is_locked(profile_id: ProfilePathId, caller: CallerDep, context: Ctx) -> dict:
    try:
        context.profiles.get_for_user(caller.user_id, profile_id)
    except ProfileError as exc:
        raise _handle(exc) from exc
    return {"profile_id": profile_id, "requires_pin":
            context.profiles.requires_pin(profile_id)}


@router.post("/select", response_model=OkResponse,
             summary="Record the profile the client is switching to")
async def select_profile(payload: ProfileSelection, caller: UncappedCallerDep,
                          context: Ctx) -> OkResponse:
    """Audit the profile switch.

    The returned access token is *not* reissued here -- the client already has
    one.  This exists so the security log records which profile a session is
    acting as, which is the first thing anyone wants to know after an incident.
    """
    try:
        context.profiles.get_for_user(caller.user_id, payload.profile_id)
    except ProfileError as exc:
        raise _handle(exc) from exc
    context.audit.record(
        "profile.selected", outcome="ok",
        user_id=caller.user_id, profile_id=payload.profile_id, ip_address=caller.ip,
    )
    return OkResponse(detail="Profile selected.")


__all__ = ["router"]