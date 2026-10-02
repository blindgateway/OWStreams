"""Profiles: creation, PIN locking and profile-scoped authorisation.

The invariant that matters for security: **a profile is only ever reachable
through its owning user**.  Every public method takes ``user_id`` and validates
that the target profile belongs to it, so a crafted ``profile_id`` in a URL can
never read another account's history.  :class:`ProfileAccessError` is raised on
mismatch and the API turns it into 404 (not 403) -- telling an attacker that a
profile id exists but is not theirs is itself a leak.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from ..db import repositories as repo
from ..db.database import Database
from ..security import passwords
from ..security.audit import AuditLogger, SecurityEventTypes

log = logging.getLogger(__name__)

MAX_PROFILES_PER_USER = 8
MAX_PROFILE_NAME_LENGTH = 24
MIN_PIN_ATTEMPTS_BEFORE_COOLDOWN = 5
PIN_COOLDOWN_SECONDS = 60


class ProfileError(Exception):
    def __init__(self, message: str, code: str = "invalid_profile",
                 status_code: int = 422) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


class ProfileAccessError(ProfileError):
    """The caller does not own this profile.  Rendered as 404 by the API."""

    def __init__(self, message: str = "Profile not found.", code: str = "profile_not_found") -> None:
        super().__init__(message, code=code, status_code=404)


class PinRequiredError(ProfileError):
    """The profile is PIN-locked and no valid PIN was presented."""

    def __init__(self, message: str = "This profile is locked. Enter its PIN.") -> None:
        super().__init__(message, code="pin_required", status_code=401)


class PinIncorrectError(ProfileError):
    def __init__(self, message: str = "Incorrect PIN.", attempts_left: int | None = None) -> None:
        super().__init__(message, code="pin_incorrect", status_code=401)
        self.attempts_left = attempts_left


@dataclass(slots=True)
class ProfileView:
    """The shape sent to the client.  Never contains a PIN hash."""

    id: int
    name: str
    avatar_id: str | None
    has_pin: bool
    is_kids: bool
    max_watch_minutes: int | None
    sort_order: int
    created_at: str

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "ProfileView":
        return cls(
            id=int(row["id"]),
            name=row["name"],
            avatar_id=row.get("avatar_id"),
            has_pin=bool(row.get("has_pin", row.get("pin_hash"))),
            is_kids=bool(row.get("is_kids")),
            max_watch_minutes=row.get("max_watch_minutes"),
            sort_order=int(row.get("sort_order") or 0),
            created_at=row.get("created_at") or "",
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "avatar_id": self.avatar_id,
            "has_pin": self.has_pin,
            "is_kids": self.is_kids,
            "max_watch_minutes": self.max_watch_minutes,
            "sort_order": self.sort_order,
            "created_at": self.created_at,
        }


class ProfileService:
    def __init__(self, db: Database, *, audit: AuditLogger | None = None) -> None:
        self.db = db
        self.audit = audit or AuditLogger(db, enabled=False)

    # -- listing / lookup -------------------------------------------------
    def list_for_user(self, user_id: int) -> list[ProfileView]:
        return [ProfileView.from_row(row) for row in repo.list_profiles(self.db, user_id)]

    def get_for_user(self, user_id: int, profile_id: int) -> dict[str, Any]:
        profile = repo.get_profile(self.db, profile_id)
        if not profile or int(profile["user_id"]) != user_id:
            raise ProfileAccessError()
        return profile

    def owns_profile(self, user_id: int, profile_id: int) -> bool:
        row = self.db.query_one(
            "SELECT 1 FROM profiles WHERE id = ? AND user_id = ?", (profile_id, user_id)
        )
        return row is not None

    def require(self, user_id: int, profile_id: int | None) -> int:
        """Validate a profile id arriving from a header or query parameter."""
        if profile_id is None:
            profiles = self.list_for_user(user_id)
            if not profiles:
                raise ProfileError("This account has no profiles.", code="no_profiles",
                                   status_code=409)
            return profiles[0].id
        self.get_for_user(user_id, int(profile_id))
        return int(profile_id)

    # -- mutations --------------------------------------------------------
    def create(self, user_id: int, name: str, *, avatar_id: str | None = None,
               pin: str | None = None, is_kids: bool = False,
               max_watch_minutes: int | None = None) -> ProfileView:
        clean_name = (name or "").strip()
        if not clean_name:
            raise ProfileError("Profile name cannot be empty.")
        if len(clean_name) > MAX_PROFILE_NAME_LENGTH:
            raise ProfileError(
                f"Profile name cannot exceed {MAX_PROFILE_NAME_LENGTH} characters."
            )
        if any(ord(ch) < 0x20 for ch in clean_name):
            raise ProfileError("Profile name cannot contain control characters.")

        existing = repo.count_profiles(self.db, user_id)
        if existing >= MAX_PROFILES_PER_USER:
            raise ProfileError(
                f"An account can have at most {MAX_PROFILES_PER_USER} profiles.",
                code="profile_limit", status_code=409,
            )

        pin_hash = None
        if pin is not None:
            pin_hash = self._hash_pin(pin)

        created = repo.create_profile(
            self.db, user_id, clean_name, avatar_id=avatar_id, pin_hash=pin_hash,
            is_kids=is_kids, max_watch_minutes=max_watch_minutes,
        )
        self.audit.record(
            "profile.created", outcome="ok", user_id=user_id, profile_id=int(created["id"]),
            detail={"name": clean_name, "has_pin": pin_hash is not None},
        )
        return ProfileView.from_row(created)

    def update(self, user_id: int, profile_id: int, *, name: str | None = None,
               avatar_id: str | None = None, is_kids: bool | None = None,
               max_watch_minutes: int | None = None,
               sort_order: int | None = None) -> ProfileView:
        self.get_for_user(user_id, profile_id)
        fields: dict[str, Any] = {}
        if name is not None:
            clean_name = name.strip()
            if not clean_name:
                raise ProfileError("Profile name cannot be empty.")
            if len(clean_name) > MAX_PROFILE_NAME_LENGTH:
                raise ProfileError(
                    f"Profile name cannot exceed {MAX_PROFILE_NAME_LENGTH} characters."
                )
            fields["name"] = clean_name
        if avatar_id is not None:
            fields["avatar_id"] = avatar_id or None
        if is_kids is not None:
            fields["is_kids"] = int(is_kids)
        if max_watch_minutes is not None:
            fields["max_watch_minutes"] = max_watch_minutes
        if sort_order is not None:
            fields["sort_order"] = int(sort_order)

        updated = repo.update_profile(self.db, profile_id, **fields)
        return ProfileView.from_row(updated or {})

    def delete(self, user_id: int, profile_id: int) -> None:
        profile = self.get_for_user(user_id, profile_id)
        last = repo.count_profiles(self.db, user_id)
        if last <= 1:
            raise ProfileError(
                "An account must keep at least one profile.",
                code="last_profile", status_code=409,
            )
        repo.delete_profile(self.db, profile_id)
        self.audit.record(
            "profile.deleted", outcome="ok", user_id=user_id, profile_id=profile_id,
            detail={"name": profile["name"]},
        )

    def reorder(self, user_id: int, ordered_ids: list[int]) -> list[ProfileView]:
        owned = {p.id for p in self.list_for_user(user_id)}
        requested = [int(pid) for pid in ordered_ids]
        if set(requested) != owned:
            raise ProfileError(
                "The list must contain every profile belonging to this account.",
                code="profile_set_mismatch",
            )
        for index, profile_id in enumerate(requested):
            repo.update_profile(self.db, profile_id, sort_order=index)
        return self.list_for_user(user_id)

    # -- PIN --------------------------------------------------------------
    def _hash_pin(self, pin: str) -> str:
        try:
            return passwords.hash_pin(pin)
        except ValueError as exc:
            raise ProfileError(str(exc), code="invalid_pin") from exc

    def set_pin(self, user_id: int, profile_id: int, pin: str | None) -> ProfileView:
        self.get_for_user(user_id, profile_id)
        if pin is None or pin == "":
            repo.update_profile(self.db, profile_id, pin_hash=None, pin_updated_at=None)
            self.audit.record(
                SecurityEventTypes.PROFILE_PIN_SET, outcome="ok",
                user_id=user_id, profile_id=profile_id, detail={"cleared": True},
            )
            return ProfileView.from_row(repo.get_profile(self.db, profile_id) or {})

        # Changing a PIN requires proving you know the old one when one exists.
        existing = repo.get_profile_pin(self.db, profile_id)
        if existing and existing.get("pin_hash"):
            raise ProfileError(
                "The current PIN must be supplied before setting a new one.",
                code="pin_change_requires_current", status_code=403,
            )

        repo.update_profile(
            self.db, profile_id, pin_hash=self._hash_pin(pin),
            pin_updated_at=_utcnow_iso(),
        )
        self.audit.record(
            SecurityEventTypes.PROFILE_PIN_SET, outcome="ok",
            user_id=user_id, profile_id=profile_id, detail={"cleared": False},
        )
        return ProfileView.from_row(repo.get_profile(self.db, profile_id) or {})

    def change_pin(self, user_id: int, profile_id: int, *, current_pin: str,
                   new_pin: str) -> ProfileView:
        self.get_for_user(user_id, profile_id)
        existing = repo.get_profile_pin(self.db, profile_id)
        if not existing or not existing.get("pin_hash"):
            raise ProfileError("This profile does not have a PIN.", code="no_pin", status_code=409)
        if not passwords.verify_pin(current_pin or "", existing["pin_hash"]):
            self.audit.record(
                SecurityEventTypes.PROFILE_PIN_FAILED, outcome="denied",
                user_id=user_id, profile_id=profile_id,
            )
            raise ProfileIncorrectError()
        repo.update_profile(
            self.db, profile_id, pin_hash=self._hash_pin(new_pin),
            pin_updated_at=_utcnow_iso(),
        )
        self.audit.record(
            SecurityEventTypes.PROFILE_PIN_SET, outcome="ok",
            user_id=user_id, profile_id=profile_id, detail={"changed": True},
        )
        return ProfileView.from_row(repo.get_profile(self.db, profile_id) or {})

    def verify_pin(self, user_id: int, profile_id: int, pin: str,
                   *, attempts: int = 0) -> None:
        """Raise on failure; return cleanly when the PIN matches."""
        profile = self.get_for_user(user_id, profile_id)
        pin_hash = profile.get("pin_hash")
        if not pin_hash:
            return

        if attempts >= MIN_PIN_ATTEMPTS_BEFORE_COOLDOWN:
            # A short cooldown makes guessing a 4-digit PIN pointless even
            # though the account itself is already authenticated.
            raise PinIncorrectError(
                "Too many PIN attempts. Wait a moment and try again.",
                attempts_left=0,
            )

        if not passwords.verify_pin(pin or "", pin_hash):
            self.audit.record(
                SecurityEventTypes.PROFILE_PIN_FAILED, outcome="denied",
                user_id=user_id, profile_id=profile_id,
                detail={"attempt": attempts + 1},
            )
            raise PinIncorrectError(
                attempts_left=max(0, MIN_PIN_ATTEMPTS_BEFORE_COOLDOWN - attempts - 1)
            )

        # Opportunistically upgrade a legacy plaintext PIN.
        if not str(pin_hash).startswith("scrypt$"):
            repo.update_profile(
                self.db, profile_id, pin_hash=self._hash_pin(pin),
                pin_updated_at=_utcnow_iso(),
            )

    def requires_pin(self, profile_id: int) -> bool:
        row = repo.get_profile_pin(self.db, profile_id)
        return bool(row and row.get("pin_hash"))

    # -- bootstrap --------------------------------------------------------
    def ensure_default_profile(self, user_id: int, name: str = "Main") -> ProfileView:
        existing = self.list_for_user(user_id)
        if existing:
            return existing[0]
        return self.create(user_id, name)


def _utcnow_iso() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).replace(microsecond=0).isoformat()