"""Request and response models.

Pydantic does the input validation that the service layer then re-checks, and
-- more importantly -- constrains what FastAPI will echo back.  Unknown fields
are rejected rather than silently dropped, so a client cannot smuggle data into
a model that gets written to the database.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# ---------------------------------------------------------------------------
# Shared
# ---------------------------------------------------------------------------
class StrictModel(BaseModel):
    """Base model: reject unknown fields, trim strings, cap their length."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    @field_validator("*", mode="after")
    @classmethod
    def _bound_strings(cls, value: Any) -> Any:
        if isinstance(value, str) and len(value) > 4000:
            raise ValueError("string field is too long")
        return value


class OkResponse(StrictModel):
    ok: bool = True
    detail: str | None = None


class ErrorResponse(BaseModel):
    """The single error shape every failure returns."""

    model_config = ConfigDict(extra="forbid")

    error: str
    message: str
    detail: Any | None = None
    request_id: str | None = None


class PageMeta(BaseModel):
    """Pagination fields, for endpoints that return a bare list.

    :class:`MediaPage` inlines the same four fields rather than nesting them, so
    a client reading ``items`` does not have to reach through an extra object.
    """

    total: int
    offset: int
    limit: int
    has_more: bool


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------
class LoginRequest(StrictModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)
    device_name: str | None = Field(default=None, max_length=128)
    client_version: str | None = Field(default=None, max_length=32)
    #: Remember this device: the refresh token then lives a year, not a month.
    remember: bool = False


class RefreshRequest(StrictModel):
    refresh_token: str = Field(min_length=8, max_length=4096)


class UserSummary(BaseModel):
    id: int
    username: str
    display_name: str
    is_admin: bool
    must_change_password: bool = False
    email: str | None = None
    last_login_at: str | None = None


class TokenResponse(BaseModel):
    access_token: str
    token_type: Literal["Bearer"] = "Bearer"
    expires_in: int
    refresh_token: str
    refresh_expires_at: str
    session_id: str
    must_change_password: bool
    user: UserSummary


class PasswordChangeRequest(StrictModel):
    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=1, max_length=256)
    logout_other_sessions: bool = True

    @field_validator("new_password")
    @classmethod
    def _must_differ(cls, value: str, info) -> str:
        current = info.data.get("current_password")
        if current and value == current:
            raise ValueError("new password must differ from the current one")
        return value


class PasswordResetRequest(StrictModel):
    new_password: str = Field(min_length=8, max_length=256)


# ---------------------------------------------------------------------------
# Server / first-run
# ---------------------------------------------------------------------------
class ServerInfo(BaseModel):
    """Unauthenticated server capabilities, used by the client's setup screen."""

    name: str = "Stream"
    version: str
    api_version: str
    requires_setup: bool
    authenticated: bool = False
    library_count: int = 0
    profile_count: int = 0
    library_roots: list[str] = Field(default_factory=list)
    ffprobe_available: bool = False
    metadata_provider: str = "local"
    #: Feature flags only -- no information about routes or privileges.
    capabilities: dict[str, bool] = Field(default_factory=dict)


class SetupRequest(StrictModel):
    """Creates the first account.  Refused once any user exists."""

    username: str = Field(min_length=3, max_length=32)
    password: str = Field(min_length=8, max_length=256)
    display_name: str | None = Field(default=None, max_length=64)
    #: Optional convenience: create a first profile at the same time.
    profile_name: str | None = Field(default=None, max_length=24)
    library_roots: list[str] = Field(default_factory=list, max_length=16)

    @field_validator("username")
    @classmethod
    def _valid_username(cls, value: str) -> str:
        import re

        if not re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]{2,31}$", value):
            raise ValueError(
                "Username must be 3-32 characters using letters, digits, dot, "
                "dash or underscore."
            )
        return value


# ---------------------------------------------------------------------------
# Profiles
# ---------------------------------------------------------------------------
class ProfileOut(BaseModel):
    id: int
    name: str
    avatar_id: str | None = None
    has_pin: bool
    is_kids: bool
    max_watch_minutes: int | None = None
    sort_order: int
    created_at: str


class ProfileCreate(StrictModel):
    name: str = Field(min_length=1, max_length=24)
    avatar_id: str | None = Field(default=None, max_length=64)
    pin: str | None = Field(default=None, pattern=r"^\d{4,8}$")
    is_kids: bool = False
    max_watch_minutes: int | None = Field(default=None, ge=0, le=24 * 60)


class ProfileUpdate(StrictModel):
    name: str | None = Field(default=None, min_length=1, max_length=24)
    avatar_id: str | None = Field(default=None, max_length=64)
    is_kids: bool | None = None
    max_watch_minutes: int | None = Field(default=None, ge=0, le=24 * 60)
    sort_order: int | None = Field(default=None, ge=0, le=100)


class ProfilePinSet(StrictModel):
    #: ``null`` clears the PIN, which on a locked profile needs ``current_pin``.
    pin: str | None = Field(default=None, pattern=r"^\d{4,8}$")
    current_pin: str | None = Field(default=None, pattern=r"^\d{4,8}$")


class ProfilePinChange(StrictModel):
    current_pin: str = Field(pattern=r"^\d{4,8}$")
    new_pin: str = Field(pattern=r"^\d{4,8}$")


class ProfilePinVerify(StrictModel):
    pin: str = Field(pattern=r"^\d{4,8}$")


class ProfileReorder(StrictModel):
    profile_ids: list[int] = Field(min_length=1, max_length=16)

    @field_validator("profile_ids")
    @classmethod
    def _unique(cls, value: list[int]) -> list[int]:
        if len(set(value)) != len(value):
            raise ValueError("profile_ids must not contain duplicates")
        return value


class ProfileSelection(StrictModel):
    """The client tells the server which profile a token is acting as."""

    profile_id: int = Field(ge=1)


# ---------------------------------------------------------------------------
# Media
# ---------------------------------------------------------------------------
class MediaOut(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: int
    slug: str
    title: str
    original_title: str | None = None
    overview: str = ""
    tagline: str | None = None
    year: int | None = None
    release_date: str | None = None
    runtime_minutes: int | None = None
    content_type: str = "movie"
    parental_rating: str | None = None
    rating_value: float | None = None
    rating_source: str | None = None
    rating_vote_count: int | None = None
    poster_key: str | None = None
    backdrop_key: str | None = None
    logo_key: str | None = None
    width: int | None = None
    height: int | None = None
    video_codec: str | None = None
    audio_codec: str | None = None
    audio_channels: int | None = None
    container: str | None = None
    file_size_bytes: int | None = None
    added_at: str | None = None
    updated_at: str | None = None
    last_played_at: str | None = None
    play_count: int = 0
    availability: str = "unknown"
    integrity_state: str = "unknown"

    genres: list[dict[str, str]] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    cast: list[dict[str, Any]] = Field(default_factory=list)
    directors: list[dict[str, Any]] = Field(default_factory=list)
    streams: dict[str, list[dict[str, Any]]] = Field(default_factory=dict)

    is_favorite: bool | None = None
    user_rating: float | None = None
    progress: dict[str, Any] | None = None


class MediaPage(BaseModel):
    items: list[MediaOut]
    total: int
    offset: int
    limit: int
    has_more: bool


class MediaDetail(MediaOut):
    overview: str = ""
    tagline: str | None = None


# ---------------------------------------------------------------------------
# Progress / ratings
# ---------------------------------------------------------------------------
class ProgressUpdate(StrictModel):
    position_seconds: float = Field(ge=0)
    duration_seconds: float | None = Field(default=None, ge=0)
    completed: bool | None = None


class ProgressOut(BaseModel):
    profile_id: int
    media_id: int
    position_seconds: float
    duration_seconds: float
    percent: float
    completed: bool


class RatingUpdate(StrictModel):
    score: float = Field(ge=0.5, le=5.0)


class RatingOut(BaseModel):
    media_id: int
    score: float


# ---------------------------------------------------------------------------
# Playback
# ---------------------------------------------------------------------------
class PlaybackTokenOut(BaseModel):
    playback_token: str
    expires_in: int
    stream_url: str
    session_id: str
    start_offset: float = 0.0


# ---------------------------------------------------------------------------
# Administration
# ---------------------------------------------------------------------------
class AdminUserCreate(StrictModel):
    username: str = Field(min_length=3, max_length=32)
    password: str = Field(min_length=8, max_length=256)
    display_name: str | None = Field(default=None, max_length=64)
    email: str | None = Field(default=None, max_length=254)
    is_admin: bool = False

    @field_validator("username")
    @classmethod
    def _valid_username(cls, value: str) -> str:
        import re

        if not re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]{2,31}$", value):
            raise ValueError("Invalid username format.")
        return value


class AdminUserOut(BaseModel):
    """One account, as an administrator sees it.

    ``extra="ignore"``, not ``"allow"``: these models are built from a raw
    ``users`` row, and ``extra="allow"`` publishes whatever else that row
    carries -- including ``password_hash``, which it does.  Declaring the
    fields here and dropping the rest means adding a column to ``users``
    cannot leak it by accident.
    """

    model_config = ConfigDict(extra="ignore")

    id: int
    username: str
    display_name: str
    email: str | None = None
    is_admin: bool
    is_active: bool
    must_change_password: bool
    created_at: str
    updated_at: str | None = None
    last_login_at: str | None = None


class LibraryRootIn(StrictModel):
    path: str = Field(min_length=1, max_length=4096)

    @field_validator("path")
    @classmethod
    def _absolute(cls, value: str) -> str:
        if not value.startswith("/"):
            raise ValueError("library roots must be absolute paths")
        if "\x00" in value:
            raise ValueError("invalid path")
        return value


class ImportRequest(StrictModel):
    paths: list[str] = Field(default_factory=list, max_length=1000)
    scan: bool = False
    remove_missing: bool = False
    reimport_all: bool = False
    refresh_metadata: bool = False


class ImportResultOut(BaseModel):
    model_config = ConfigDict(extra="allow")

    added: int = 0
    updated: int = 0
    skipped: int = 0
    removed: int = 0
    failed: int = 0
    probed: int = 0
    metadata_hits: int = 0
    duration_seconds: float = 0.0
    errors: list[str] = Field(default_factory=list)


class MediaPatch(StrictModel):
    """Operator edits to a media row.  Every field is optional; only supplied
    fields are written."""

    title: str | None = Field(default=None, min_length=1, max_length=200)
    original_title: str | None = Field(default=None, max_length=200)
    overview: str | None = Field(default=None, max_length=8000)
    tagline: str | None = Field(default=None, max_length=300)
    year: int | None = Field(default=None, ge=1888, le=2200)
    release_date: str | None = Field(default=None, max_length=10)
    runtime_minutes: int | None = Field(default=None, ge=0, le=1000)
    parental_rating: str | None = Field(default=None, max_length=32)
    rating_value: float | None = Field(default=None, ge=0, le=10)
    rating_source: str | None = Field(default=None, max_length=32)
    content_type: Literal["movie", "series", "episode"] | None = None
    hidden: bool | None = None

    @field_validator("release_date")
    @classmethod
    def _date_shape(cls, value: str | None) -> str | None:
        """Accept ``YYYY`` or ``YYYY-MM-DD``, and only if the date exists.

        A regex alone lets "2020-13-45" through, which would then be written to
        the library and shown to a viewer as a release date.
        """
        import datetime as _dt

        if value is None:
            return None
        parts = value.split("-")
        if len(parts) == 1:
            if len(parts[0]) != 4 or not parts[0].isdigit():
                raise ValueError("release_date must be YYYY or YYYY-MM-DD")
            if not 1888 <= int(parts[0]) <= 2200:
                raise ValueError("year is out of range")
            return value
        if len(parts) != 3 or not all(p.isdigit() for p in parts):
            raise ValueError("release_date must be YYYY or YYYY-MM-DD")
        try:
            _dt.date(int(parts[0]), int(parts[1]), int(parts[2]))
        except ValueError as exc:
            raise ValueError(f"release_date is not a real date: {exc}") from exc
        return value


class SessionOut(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    device_name: str | None = None
    client_version: str | None = None
    ip_address: str | None = None
    created_at: str
    last_used_at: str
    expires_at: str
    revoked_at: str | None = None


class HealthOut(BaseModel):
    status: Literal["ok", "degraded"]
    version: str
    api_version: str
    database: str
    library_count: int
    missing_files: int
    uptime_seconds: float
    library_roots: list[str]
    ffprobe_available: bool


#: Path-parameter bounds.  Routes should declare these instead of a bare
#: ``int`` so an id of 0 or -1 is rejected by validation instead of being
#: compared against the database -- and so the bound shows up in the OpenAPI
#: document.
AdminId = Annotated[int, Field(ge=1)]
ProfilePathId = Annotated[int, Field(ge=1)]
MediaPathId = Annotated[int, Field(ge=1)]