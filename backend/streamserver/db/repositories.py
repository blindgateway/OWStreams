"""All SQL in the project lives here.

Rules that keep this layer honest:

* **No string interpolation of caller data.**  Every value is a bound
  parameter.  The only interpolated fragments are whitelisted column/sort
  names, resolved through :data:`SORT_COLUMNS` and friends.
* **No N+1 queries.**  Anything rendering a list joins eagerly or uses a
  single grouped query; :func:`attach_relations` is the one place that batches.
* **Every list query is paginated** and every filter that is not indexed is
  either dropped or has an index added alongside it.
* Row -> dict conversion is explicit so the API layer never leaks an internal
  column (notably ``media_path``) by accident.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from .database import Database, utcnow


class RepositoryError(LookupError):
    """A requested row does not exist."""


# ---------------------------------------------------------------------------
# Row helpers
# ---------------------------------------------------------------------------
def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return None if row is None else dict(row)


def rows_to_dicts(rows: Iterable[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(row) for row in rows]


def _json_field(raw: str | None, fallback: Any) -> Any:
    if not raw:
        return fallback
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return fallback


def _json_text(value: Any) -> str:
    if value is None:
        return "{}"
    if isinstance(value, str):
        return value
    return json.dumps(value, separators=(",", ":"), sort_keys=True, ensure_ascii=False)


#: Columns the client may see on a media row.  ``media_path`` is deliberately
#: absent: filesystem locations are an internal detail and leaking one tells an
#: attacker about the server's disk layout for no benefit.
MEDIA_PUBLIC_COLUMNS = """
    id, slug, title, original_title, sort_title, overview, tagline, year,
    release_date, runtime_minutes, content_type, parental_rating,
    rating_value, rating_source, rating_vote_count, file_size_bytes,
    video_codec, video_profile, width, height, aspect_ratio, frame_rate,
    bitrate_bps, hdr_format, color_space, audio_codec, audio_channels,
    audio_languages, subtitle_languages, container,
    poster_key, backdrop_key, logo_key, parent_media_id, source,
    added_at, updated_at, last_played_at, play_count, availability,
    integrity_state
"""

#: Whitelist for the ``sort`` query parameter.
SORT_COLUMNS: dict[str, str] = {
    "title": "m.sort_title",
    "added": "m.added_at",
    "year": "m.year",
    "rating": "m.rating_value",
    "popularity": "m.play_count",
    "last_played": "m.last_played_at",
    "runtime": "m.runtime_minutes",
    "release_date": "m.release_date",
}

DIRECTIONS = {"asc": "ASC", "desc": "DESC"}

#: Whitelist for grouping.
GROUP_BY = {
    "genre": "g.name",
    "year": "m.year",
    "type": "m.content_type",
    "rating": "m.parental_rating",
    "decade": "CAST(m.year / 10 * 10 AS INTEGER)",
    "language": "m.audio_languages",
}

CONTENT_TYPES = ("movie", "series", "episode")


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------
def get_user(db: Database, user_id: int) -> dict[str, Any] | None:
    return row_to_dict(db.query_one("SELECT * FROM users WHERE id = ?", (user_id,)))


def get_user_by_username(db: Database, username: str) -> dict[str, Any] | None:
    return row_to_dict(
        db.query_one("SELECT * FROM users WHERE username = ? COLLATE NOCASE", (username,))
    )


def get_user_by_email(db: Database, email: str) -> dict[str, Any] | None:
    return row_to_dict(db.query_one("SELECT * FROM users WHERE email = ? COLLATE NOCASE", (email,)))


def list_users(db: Database, *, include_inactive: bool = False) -> list[dict[str, Any]]:
    where = "" if include_inactive else "WHERE is_active = 1"
    return rows_to_dicts(db.query(f"SELECT * FROM users {where} ORDER BY id"))


def create_user(
    db: Database,
    *,
    username: str,
    password_hash: str,
    is_admin: bool = False,
    email: str | None = None,
    display_name: str = "",
    must_change_password: bool = False,
) -> dict[str, Any]:
    with db.write() as conn:
        cur = conn.execute(
            "INSERT INTO users (username, email, display_name, password_hash, "
            "is_admin, must_change_password, password_changed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                username,
                email,
                display_name or username,
                password_hash,
                int(is_admin),
                int(must_change_password),
                None if must_change_password else utcnow(),
            ),
        )
        user_id = int(cur.lastrowid or 0)
    return get_user(db, user_id) or {}


#: Writable user columns, mapped to the SQLite type each accepts.  A value of
#: the wrong type is a programming error, not a runtime surprise three layers
#: up in the API layer.
USER_FIELDS: dict[str, type] = {
    "email": str,
    "display_name": str,
    "password_hash": str,
    "password_changed_at": str,
    "must_change_password": int,
    "is_admin": int,
    "is_active": int,
    "autoplay": str,
    "preferred_audio_lang": str,
    "preferred_subtitle_lang": str,
    "last_login_at": str,
    "playback_preference": str,
}

#: Fields whose falsy value is meaningful and must still be written.  Every
#: other field treats 0/""/False as "not supplied", because ``update_user``
#: is called with ``**fields`` built from optional request bodies where an
#: absent key and a default value are indistinguishable.
_USER_BOOL_FIELDS = {"must_change_password", "is_admin", "is_active"}


def _normalise_user_value(field: str, value: Any) -> Any:
    if field in _USER_BOOL_FIELDS:
        return int(bool(value))
    return value


def update_user(db: Database, user_id: int, **fields: Any) -> dict[str, Any] | None:
    """Update the supplied columns of one user.

    Booleans are coerced to 0/1 because SQLite has no boolean type and the
    callers pass Python ``bool``.  Without that, a boolean binds as 1/0 but a
    column declared INTEGER still receives it -- however ``is_admin=0`` would
    be indistinguishable from "not supplied" if falsy values were dropped
    wholesale, which is why only the boolean columns survive that filter.
    """
    updates = {
        key: _normalise_user_value(key, value)
        for key, value in fields.items()
        if key in USER_FIELDS and (value is not None or key in _USER_BOOL_FIELDS)
    }
    if not updates:
        return get_user(db, user_id)

    set_clause = ", ".join(f"{key} = ?" for key in updates)
    # updated_at is set by SQLite's own datetime(), so there is no extra binding.
    db.execute(
        f"UPDATE users SET {set_clause}, updated_at = datetime('now') WHERE id = ?",
        tuple(updates.values()) + (user_id,),
    )
    return get_user(db, user_id)


def delete_user(db: Database, user_id: int) -> bool:
    result = db.execute("DELETE FROM users WHERE id = ?", (user_id,))
    return result.changes > 0


def touch_last_login(db: Database, user_id: int) -> None:
    db.execute("UPDATE users SET last_login_at = datetime('now') WHERE id = ?", (user_id,))


# ---------------------------------------------------------------------------
# Profiles
# ---------------------------------------------------------------------------
def list_profiles(db: Database, user_id: int) -> list[dict[str, Any]]:
    return rows_to_dicts(
        db.query(
            "SELECT id, user_id, name, avatar_id, is_kids, max_watch_minutes, "
            "sort_order, created_at, pin_updated_at, "
            "(pin_hash IS NOT NULL) AS has_pin "
            "FROM profiles WHERE user_id = ? ORDER BY sort_order, id",
            (user_id,),
        )
    )


def get_profile(db: Database, profile_id: int) -> dict[str, Any] | None:
    return row_to_dict(
        db.query_one(
            "SELECT p.*, u.username AS owner_username, u.is_active AS owner_active "
            "FROM profiles p JOIN users u ON u.id = p.user_id WHERE p.id = ?",
            (profile_id,),
        )
    )


def get_profile_pin(db: Database, profile_id: int) -> dict[str, Any] | None:
    """The one query that may return a PIN hash -- never exposed via the API."""
    return row_to_dict(
        db.query_one("SELECT id, user_id, name, pin_hash FROM profiles WHERE id = ?",
                     (profile_id,))
    )


class DuplicateNameError(ValueError):
    """A unique (user_id, name) constraint was violated."""


def create_profile(
    db: Database,
    user_id: int,
    name: str,
    *,
    avatar_id: str | None = None,
    pin_hash: str | None = None,
    is_kids: bool = False,
    max_watch_minutes: int | None = None,
    sort_order: int | None = None,
) -> dict[str, Any]:
    if sort_order is None:
        row = db.query_one("SELECT COALESCE(MAX(sort_order), -1) + 1 FROM profiles WHERE user_id = ?",
                           (user_id,))
        sort_order = int(row[0]) if row else 0
    try:
        with db.write() as conn:
            cur = conn.execute(
                "INSERT INTO profiles (user_id, name, avatar_id, pin_hash, pin_updated_at, "
                "is_kids, max_watch_minutes, sort_order) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    user_id, name, avatar_id, pin_hash,
                    utcnow() if pin_hash else None,
                    int(is_kids), max_watch_minutes, sort_order,
                ),
            )
            profile_id = int(cur.lastrowid or 0)
    except sqlite3.IntegrityError as exc:
        # The schema enforces UNIQUE (user_id, name); translating it here means
        # the API can answer 409 instead of leaking a 500.
        if "UNIQUE" in str(exc).upper():
            raise DuplicateNameError(f"a profile named {name!r} already exists") from exc
        raise
    return get_profile(db, profile_id) or {}


def update_profile(db: Database, profile_id: int, **fields: Any) -> dict[str, Any] | None:
    allowed = {"name", "avatar_id", "pin_hash", "pin_updated_at", "is_kids",
               "max_watch_minutes", "sort_order"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return get_profile(db, profile_id)
    set_clause = ", ".join(f"{k} = ?" for k in updates)
    db.execute(
        f"UPDATE profiles SET {set_clause} WHERE id = ?",
        tuple(updates.values()) + (profile_id,),
    )
    return get_profile(db, profile_id)


def delete_profile(db: Database, profile_id: int) -> bool:
    result = db.execute("DELETE FROM profiles WHERE id = ?", (profile_id,))
    return result.changes > 0


def count_profiles(db: Database, user_id: int) -> int:
    row = db.query_one("SELECT COUNT(*) FROM profiles WHERE user_id = ?", (user_id,))
    return int(row[0]) if row else 0


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------
def create_session(
    db: Database,
    *,
    session_id: str,
    user_id: int,
    refresh_token_hash: str,
    expires_at: str,
    device_name: str | None = None,
    client_version: str | None = None,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> dict[str, Any]:
    db.execute(
        "INSERT INTO sessions (id, user_id, refresh_token_hash, device_name, "
        "client_version, ip_address, user_agent, expires_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (session_id, user_id, refresh_token_hash, device_name, client_version,
         ip_address, user_agent, expires_at),
    )
    row = db.query_one("SELECT * FROM sessions WHERE id = ?", (session_id,))
    return dict(row) if row else {}


def get_session(db: Database, session_id: str) -> dict[str, Any] | None:
    return row_to_dict(db.query_one("SELECT * FROM sessions WHERE id = ?", (session_id,)))


def get_session_by_token(db: Database, token_hash: str) -> dict[str, Any] | None:
    return row_to_dict(
        db.query_one(
            "SELECT * FROM sessions WHERE refresh_token_hash = ? "
            "AND revoked_at IS NULL AND expires_at > datetime('now')",
            (token_hash,),
        )
    )


def list_sessions(db: Database, user_id: int) -> list[dict[str, Any]]:
    return rows_to_dicts(
        db.query(
            "SELECT id, device_name, client_version, ip_address, created_at, "
            "last_used_at, expires_at, revoked_at FROM sessions "
            "WHERE user_id = ? ORDER BY (revoked_at IS NULL) DESC, last_used_at DESC",
            (user_id,),
        )
    )


def rotate_session(
    db: Database,
    session_id: str,
    *,
    refresh_token_hash: str,
    expires_at: str,
    ip_address: str | None = None,
) -> None:
    db.execute(
        "UPDATE sessions SET refresh_token_hash = ?, expires_at = ?, "
        "ip_address = COALESCE(?, ip_address), last_used_at = datetime('now') "
        "WHERE id = ?",
        (refresh_token_hash, expires_at, ip_address, session_id),
    )


def revoke_session(db: Database, session_id: str, reason: str = "logout") -> bool:
    result = db.execute(
        "UPDATE sessions SET revoked_at = datetime('now'), revoked_reason = ? "
        "WHERE id = ? AND revoked_at IS NULL",
        (reason, session_id),
    )
    return result.changes > 0


def revoke_all_sessions(db: Database, user_id: int, *, except_session: str | None = None,
                        reason: str = "logout_all") -> int:
    if except_session:
        result = db.execute(
            "UPDATE sessions SET revoked_at = datetime('now'), revoked_reason = ? "
            "WHERE user_id = ? AND revoked_at IS NULL AND id != ?",
            (reason, user_id, except_session),
        )
    else:
        result = db.execute(
            "UPDATE sessions SET revoked_at = datetime('now'), revoked_reason = ? "
            "WHERE user_id = ? AND revoked_at IS NULL",
            (reason, user_id),
        )
    return result.changes


def prune_sessions(db: Database, keep_days: int = 30) -> int:
    result = db.execute(
        "DELETE FROM sessions WHERE expires_at < datetime('now', ?)",
        (f"-{keep_days} days",),
    )
    return result.changes


# ---------------------------------------------------------------------------
# Genres / tags / people
# ---------------------------------------------------------------------------
def get_or_create_genre(db: Database, name: str, slug: str) -> int:
    row = db.query_one("SELECT id FROM genres WHERE slug = ?", (slug,))
    if row:
        return int(row["id"])
    result = db.execute(
        "INSERT INTO genres (name, slug) VALUES (?, ?) ON CONFLICT(slug) DO NOTHING", (name, slug)
    )
    if result.rowid:
        return result.rowid
    row = db.query_one("SELECT id FROM genres WHERE slug = ?", (slug,))
    return int(row["id"]) if row else 0


def get_or_create_tag(db: Database, name: str, slug: str) -> int:
    row = db.query_one("SELECT id FROM tags WHERE slug = ?", (slug,))
    if row:
        return int(row["id"])
    result = db.execute(
        "INSERT INTO tags (name, slug) VALUES (?, ?) ON CONFLICT(slug) DO NOTHING", (name, slug)
    )
    if result.rowid:
        return result.rowid
    row = db.query_one("SELECT id FROM tags WHERE slug = ?", (slug,))
    return int(row["id"]) if row else 0


def get_or_create_person(
    db: Database, name: str, slug: str, role: str = "cast",
    *, tmdb_id: str | None = None, imdb_id: str | None = None,
) -> int:
    row = db.query_one("SELECT id FROM people WHERE name = ? AND role = ?", (name, role))
    if row:
        person_id = int(row["id"])
        if tmdb_id or imdb_id:
            db.execute(
                "UPDATE people SET tmdb_id = COALESCE(tmdb_id, ?), "
                "imdb_id = COALESCE(imdb_id, ?) WHERE id = ?",
                (tmdb_id, imdb_id, person_id),
            )
        return person_id
    result = db.execute(
        "INSERT INTO people (name, slug, role, tmdb_id, imdb_id) VALUES (?, ?, ?, ?, ?)",
        (name, slug, role, tmdb_id, imdb_id),
    )
    return result.rowid


def set_media_genres(db: Database, media_id: int, genres: Sequence[tuple[str, str]]) -> None:
    db.execute("DELETE FROM media_genres WHERE media_id = ?", (media_id,))
    for name, slug in genres:
        genre_id = get_or_create_genre(db, name, slug)
        if genre_id:
            db.execute(
                "INSERT OR IGNORE INTO media_genres (media_id, genre_id) VALUES (?, ?)",
                (media_id, genre_id),
            )


def set_media_tags(db: Database, media_id: int, tags: Sequence[tuple[str, str]]) -> None:
    db.execute("DELETE FROM media_tags WHERE media_id = ?", (media_id,))
    for name, slug in tags:
        tag_id = get_or_create_tag(db, name, slug)
        if tag_id:
            db.execute(
                "INSERT OR IGNORE INTO media_tags (media_id, tag_id) VALUES (?, ?)",
                (media_id, tag_id),
            )


def set_media_credits(
    db: Database, media_id: int, credits: Sequence[dict[str, Any]]
) -> None:
    db.execute("DELETE FROM media_credits WHERE media_id = ?", (media_id,))
    for index, credit in enumerate(credits):
        name = (credit.get("name") or "").strip()
        if not name:
            continue
        role = credit.get("role") or "cast"
        person_id = get_or_create_person(
            db, name, credit.get("slug") or _slugify_name(name), role,
            tmdb_id=credit.get("tmdb_id"), imdb_id=credit.get("imdb_id"),
        )
        if not person_id:
            continue
        db.execute(
            "INSERT OR REPLACE INTO media_credits (media_id, person_id, character, job, ord) "
            "VALUES (?, ?, ?, ?, ?)",
            (media_id, person_id, credit.get("character"), credit.get("job"), index),
        )


def _slugify_name(name: str) -> str:
    from ..security.paths import slugify

    return slugify(name, max_length=60, fallback="person")


def set_media_streams(db: Database, media_id: int, streams: Sequence[dict[str, Any]]) -> None:
    db.execute("DELETE FROM media_streams WHERE media_id = ?", (media_id,))
    for stream in streams:
        db.execute(
            "INSERT OR REPLACE INTO media_streams "
            "(media_id, stream_id, kind, index_no, language, title, codec, channels, "
            " channel_layout, is_default, is_forced, external_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                media_id,
                int(stream.get("stream_id", stream.get("index", 0))),
                stream.get("kind", "video"),
                int(stream.get("index_no", stream.get("stream_id", 0))),
                stream.get("language"),
                stream.get("title"),
                stream.get("codec"),
                stream.get("channels"),
                stream.get("channel_layout"),
                int(bool(stream.get("is_default"))),
                int(bool(stream.get("is_forced"))),
                stream.get("external_id"),
            ),
        )


def set_external_ids(db: Database, media_id: int, ids: dict[str, Any]) -> None:
    db.execute("DELETE FROM media_external_ids WHERE media_id = ?", (media_id,))
    for provider, value in (ids or {}).items():
        if value in (None, ""):
            continue
        db.execute(
            "INSERT OR REPLACE INTO media_external_ids (media_id, provider, value) "
            "VALUES (?, ?, ?)",
            (media_id, str(provider)[:32], str(value)[:128]),
        )


def get_external_id(db: Database, provider: str, value: str) -> dict[str, Any] | None:
    return row_to_dict(
        db.query_one(
            "SELECT m.* FROM media m JOIN media_external_ids e ON e.media_id = m.id "
            "WHERE e.provider = ? AND e.value = ?",
            (provider, value),
        )
    )


# ---------------------------------------------------------------------------
# Media
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class MediaFilter:
    """Everything the list endpoints accept, already validated by the API."""

    content_type: str | None = None
    genres: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    years: tuple[int | None, int | None] = (None, None)
    decade: int | None = None
    min_rating: float | None = None
    max_rating: float | None = None
    min_runtime: int | None = None
    max_runtime: int | None = None
    resolution: str | None = None        # "4k" | "1080p" | "720p"
    hdr_only: bool | False = False
    parental_rating: str | None = None
    person_id: int | None = None
    collection_id: int | None = None
    added_after: str | None = None
    added_before: str | None = None
    available_only: bool = True
    hide_missing: bool = False
    sort: str = "added"
    direction: str = "desc"
    limit: int = 50
    offset: int = 0


def _build_media_where(f: MediaFilter, *, alias: str = "m") -> tuple[str, list[Any]]:
    clauses: list[str] = []
    params: list[Any] = []

    clauses.append(f"{alias}.hidden = 0")
    if f.content_type:
        if f.content_type not in CONTENT_TYPES:
            raise ValueError(f"unknown content_type {f.content_type!r}")
        clauses.append(f"{alias}.content_type = ?")
        params.append(f.content_type)

    if f.available_only:
        clauses.append(f"{alias}.integrity_state != 'missing'")
    if f.hide_missing:
        clauses.append(f"{alias}.integrity_state = 'ok'")

    if f.genres:
        placeholders = ",".join("?" * len(f.genres))
        clauses.append(
            f"{alias}.id IN (SELECT mg.media_id FROM media_genres mg "
            f"JOIN genres g ON g.id = mg.genre_id WHERE g.slug IN ({placeholders}))"
        )
        params.extend(f.genres)

    if f.tags:
        placeholders = ",".join("?" * len(f.tags))
        clauses.append(
            f"{alias}.id IN (SELECT mt.media_id FROM media_tags mt "
            f"JOIN tags t ON t.id = mt.tag_id WHERE t.slug IN ({placeholders}))"
        )
        params.extend(f.tags)

    if f.person_id:
        clauses.append(
            f"{alias}.id IN (SELECT media_id FROM media_credits WHERE person_id = ?)"
        )
        params.append(f.person_id)

    if f.collection_id:
        clauses.append(
            f"{alias}.id IN (SELECT media_id FROM collection_media WHERE collection_id = ?)"
        )
        params.append(f.collection_id)

    low, high = f.years
    if low is not None:
        clauses.append(f"{alias}.year >= ?")
        params.append(low)
    if high is not None:
        clauses.append(f"{alias}.year <= ?")
        params.append(high)
    if f.decade is not None:
        clauses.append(f"{alias}.year >= ? AND {alias}.year < ?")
        params.extend([f.decade, f.decade + 10])

    if f.min_rating is not None:
        clauses.append(f"{alias}.rating_value >= ?")
        params.append(f.min_rating)
    if f.max_rating is not None:
        clauses.append(f"{alias}.rating_value <= ?")
        params.append(f.max_rating)
    if f.min_runtime is not None:
        clauses.append(f"{alias}.runtime_minutes >= ?")
        params.append(f.min_runtime)
    if f.max_runtime is not None:
        clauses.append(f"{alias}.runtime_minutes <= ?")
        params.append(f.max_runtime)

    if f.resolution == "4k":
        clauses.append(f"{alias}.height >= 2000")
    elif f.resolution == "2160p":
        clauses.append(f"{alias}.height >= 2000")
    elif f.resolution == "1440p":
        clauses.append(f"{alias}.height >= 1400 AND {alias}.height < 2000")
    elif f.resolution == "1080p":
        clauses.append(f"{alias}.height >= 1000 AND {alias}.height < 1400")
    elif f.resolution == "720p":
        clauses.append(f"{alias}.height >= 700 AND {alias}.height < 1000")
    elif f.resolution == "sd":
        clauses.append(f"{alias}.height < 700")

    if f.hdr_only:
        clauses.append(f"{alias}.hdr_format IS NOT NULL AND {alias}.hdr_format != ''")
    if f.parental_rating:
        clauses.append(f"{alias}.parental_rating = ?")
        params.append(f.parental_rating)
    if f.added_after:
        clauses.append(f"{alias}.added_at >= ?")
        params.append(f.added_after)
    if f.added_before:
        clauses.append(f"{alias}.added_at <= ?")
        params.append(f.added_before)

    return (" AND ".join(clauses), params)


def list_media(db: Database, f: MediaFilter) -> list[dict[str, Any]]:
    where, params = _build_media_where(f)
    order_by = SORT_COLUMNS.get(f.sort, "m.added_at")
    direction = DIRECTIONS.get(f.direction.lower(), "DESC")
    # NULLs last in both directions keeps "year" and "rating" sorts sane.
    sql = (
        f"SELECT {', '.join('m.' + c.strip() for c in MEDIA_PUBLIC_COLUMNS.split(',') if c.strip())} "
        f"FROM media m WHERE {where} "
        f"ORDER BY {order_by} IS NULL, {order_by} {direction}, m.id {direction} "
        f"LIMIT ? OFFSET ?"
    )
    return rows_to_dicts(db.query(sql, tuple(params) + (max(1, min(f.limit, 200)), max(0, f.offset))))


def count_media(db: Database, f: MediaFilter) -> int:
    where, params = _build_media_where(f)
    row = db.query_one(f"SELECT COUNT(*) FROM media m WHERE {where}", tuple(params))
    return int(row[0]) if row else 0


def get_media(db: Database, media_id: int) -> dict[str, Any] | None:
    cols = ", ".join("m." + c.strip() for c in MEDIA_PUBLIC_COLUMNS.split(",") if c.strip())
    return row_to_dict(db.query_one(f"SELECT {cols} FROM media m WHERE m.id = ?", (media_id,)))


def get_media_full(db: Database, media_id: int) -> dict[str, Any] | None:
    """``get_media`` plus the internal path -- server-side use only."""
    row = db.query_one("SELECT * FROM media WHERE id = ?", (media_id,))
    return row_to_dict(row)


def get_media_by_slug(db: Database, slug: str) -> dict[str, Any] | None:
    row = db.query_one("SELECT * FROM media WHERE slug = ?", (slug,))
    return row_to_dict(row)


def get_media_by_path(db: Database, path: str) -> dict[str, Any] | None:
    return row_to_dict(db.query_one("SELECT * FROM media WHERE media_path = ?", (path,)))


def slug_taken(db: Database, slug: str, *, exclude_id: int | None = None) -> bool:
    if exclude_id is not None:
        row = db.query_one("SELECT 1 FROM media WHERE slug = ? AND id != ?", (slug, exclude_id))
    else:
        row = db.query_one("SELECT 1 FROM media WHERE slug = ?", (slug,))
    return row is not None


def unique_slug(db: Database, base: str, *, exclude_id: int | None = None) -> str:
    candidate = base
    suffix = 2
    while slug_taken(db, candidate, exclude_id=exclude_id):
        candidate = f"{base}-{suffix}"
        suffix += 1
        if suffix > 10_000:  # pathological; keep it bounded
            return f"{base}-{suffix}"
    return candidate


def create_media(db: Database, values: dict[str, Any]) -> int:
    columns = [
        "slug", "title", "original_title", "sort_title", "overview", "tagline",
        "year", "release_date", "runtime_minutes", "content_type", "parental_rating",
        "rating_value", "rating_source", "rating_vote_count",
        "media_path", "media_root", "file_size_bytes", "file_modified_at", "file_inode",
        "video_codec", "video_profile", "width", "height", "aspect_ratio",
        "frame_rate", "bitrate_bps", "hdr_format", "color_space",
        "audio_codec", "audio_channels", "audio_languages", "subtitle_languages",
        "container", "poster_key", "backdrop_key", "logo_key",
        "parent_media_id", "source", "external_ids", "extra", "searchable_text",
        "availability",
    ]
    present = [c for c in columns if c in values]
    placeholders = ",".join("?" * len(present))
    payload = tuple(
        _json_text(values[c]) if c in {"external_ids", "extra"} else values[c]
        for c in present
    )
    result = db.execute(
        f"INSERT INTO media ({','.join(present)}) VALUES ({placeholders})", payload
    )
    return result.rowid


def update_media(db: Database, media_id: int, **values: Any) -> dict[str, Any] | None:
    if not values:
        return get_media(db, media_id)
    assignments = []
    params: list[Any] = []
    for key, value in values.items():
        assignments.append(f"{key} = ?")
        params.append(_json_text(value) if key in {"external_ids", "extra"} else value)
    params.extend([utcnow(), media_id])
    db.execute(
        f"UPDATE media SET {', '.join(assignments)}, updated_at = ? WHERE id = ?",
        tuple(params),
    )
    return get_media(db, media_id)


def delete_media(db: Database, media_id: int) -> bool:
    result = db.execute("DELETE FROM media WHERE id = ?", (media_id,))
    return result.changes > 0


def increment_play_count(db: Database, media_id: int) -> None:
    db.execute(
        "UPDATE media SET play_count = play_count + 1, last_played_at = datetime('now') "
        "WHERE id = ?",
        (media_id,),
    )


def all_media_paths(db: Database) -> dict[str, int]:
    """``{absolute_path: media_id}`` for the integrity checker and scanner."""
    return {
        row["media_path"]: int(row["id"])
        for row in db.query("SELECT id, media_path FROM media")
    }


def media_by_root(db: Database, root: str) -> list[dict[str, Any]]:
    return rows_to_dicts(db.query("SELECT * FROM media WHERE media_root = ?", (root,)))


# ---------------------------------------------------------------------------
# Relations (batched -- no N+1)
# ---------------------------------------------------------------------------
def attach_relations(db: Database, media: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Attach genres, tags and cast to a list of media dicts with 3 queries."""
    if not media:
        return media
    ids = [int(item["id"]) for item in media]
    placeholders = ",".join("?" * len(ids))

    genres_by_media: dict[int, list[dict[str, Any]]] = {}
    for row in db.query(
        f"SELECT mg.media_id, g.name, g.slug FROM media_genres mg "
        f"JOIN genres g ON g.id = mg.genre_id WHERE mg.media_id IN ({placeholders}) "
        f"ORDER BY g.name",
        tuple(ids),
    ):
        genres_by_media.setdefault(int(row["media_id"]), []).append(
            {"name": row["name"], "slug": row["slug"]}
        )

    tags_by_media: dict[int, list[str]] = {}
    for row in db.query(
        f"SELECT mt.media_id, t.name FROM media_tags mt "
        f"JOIN tags t ON t.id = mt.tag_id WHERE mt.media_id IN ({placeholders}) ORDER BY t.name",
        tuple(ids),
    ):
        tags_by_media.setdefault(int(row["media_id"]), []).append(row["name"])

    cast_by_media: dict[int, list[dict[str, Any]]] = {}
    for row in db.query(
        f"SELECT mc.media_id, p.id AS person_id, p.name, mc.character, mc.job "
        f"FROM media_credits mc JOIN people p ON p.id = mc.person_id "
        f"WHERE mc.media_id IN ({placeholders}) AND p.role = 'cast' ORDER BY mc.ord LIMIT 3000",
        tuple(ids),
    ):
        cast_by_media.setdefault(int(row["media_id"]), []).append(
            {"id": int(row["person_id"]), "name": row["name"], "character": row["character"]}
        )

    directors_by_media: dict[int, list[dict[str, Any]]] = {}
    for row in db.query(
        f"SELECT mc.media_id, p.id AS person_id, p.name FROM media_credits mc "
        f"JOIN people p ON p.id = mc.person_id "
        f"WHERE mc.media_id IN ({placeholders}) AND p.role = 'director' ORDER BY mc.ord",
        tuple(ids),
    ):
        directors_by_media.setdefault(int(row["media_id"]), []).append(
            {"id": int(row["person_id"]), "name": row["name"]}
        )

    for item in media:
        media_id = int(item["id"])
        item["genres"] = genres_by_media.get(media_id, [])
        item["tags"] = tags_by_media.get(media_id, [])
        item["cast"] = cast_by_media.get(media_id, [])[:20]
        item["directors"] = directors_by_media.get(media_id, [])
    return media


def attach_relations_one(db: Database, item: dict[str, Any]) -> dict[str, Any]:
    return attach_relations(db, [item])[0]


def get_media_genre_slugs(db: Database, media_id: int) -> list[str]:
    return [
        row["slug"]
        for row in db.query(
            "SELECT g.slug FROM media_genres mg JOIN genres g ON g.id = mg.genre_id "
            "WHERE mg.media_id = ? ORDER BY g.name",
            (media_id,),
        )
    ]


def get_media_streams(db: Database, media_id: int) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {"video": [], "audio": [], "subtitle": []}
    for row in db.query(
        "SELECT * FROM media_streams WHERE media_id = ? ORDER BY kind, index_no",
        (media_id,),
    ):
        result.setdefault(row["kind"], []).append(dict(row))
    return result


# ---------------------------------------------------------------------------
# Full-text search
# ---------------------------------------------------------------------------
def rebuild_search_index(db: Database, media_id: int) -> None:
    """Re-derive one row of the FTS index from the relational tables.

    ``contentless_delete=1`` lets an old row be removed with a plain DELETE,
    which a contentless FTS5 table otherwise refuses.  If a SQLite build lacks
    that option the rebuild falls back to the command set that is available.
    """
    row = db.query_one(
        """
        SELECT m.id, m.title, m.original_title, m.overview, m.tagline,
               (SELECT group_concat(g.name, ' ') FROM media_genres mg
                  JOIN genres g ON g.id = mg.genre_id WHERE mg.media_id = m.id) AS genres,
               (SELECT group_concat(t.name, ' ') FROM media_tags mt
                  JOIN tags t ON t.id = mt.tag_id WHERE mt.media_id = m.id) AS tags,
               (SELECT group_concat(p.name, ' ') FROM media_credits mc
                  JOIN people p ON p.id = mc.person_id WHERE mc.media_id = m.id
                    AND p.role = 'cast') AS cast,
               (SELECT group_concat(p.name, ' ') FROM media_credits mc
                  JOIN people p ON p.id = mc.person_id WHERE mc.media_id = m.id
                    AND p.role = 'director') AS directors
          FROM media m WHERE m.id = ?
        """,
        (media_id,),
    )
    if not row:
        _delete_fts_row(db, media_id)
        return

    values = (
        int(row["id"]), row["title"] or "", row["original_title"] or "",
        row["overview"] or "", row["tagline"] or "", row["genres"] or "",
        row["tags"] or "", row["cast"] or "", row["directors"] or "",
    )
    _delete_fts_row(db, int(row["id"]))
    db.execute(
        "INSERT INTO media_fts (rowid, title, original_title, overview, tagline, "
        "genres, tags, cast, directors) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        values,
    )


def _delete_fts_row(db: Database, media_id: int) -> None:
    """Remove one indexed row, tolerating older SQLite builds.

    The table is created with ``contentless_delete=1`` so this is a plain
    DELETE.  When that option is not available (SQLite < 3.43) the standard
    workaround is to insert a row of empty strings before deleting it.
    """
    try:
        db.execute("DELETE FROM media_fts WHERE rowid = ?", (media_id,))
    except sqlite3.OperationalError as exc:
        if "contentless" not in str(exc).lower():
            raise
        empty = ("",) * 8
        db.execute(
            "INSERT INTO media_fts (media_fts, rowid, title, original_title, overview, "
            "tagline, genres, tags, cast, directors) "
            "VALUES ('delete', ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (media_id, *empty),
        )


def rebuild_all_search_index(db: Database) -> int:
    count = 0
    for row in db.query("SELECT id FROM media"):
        rebuild_search_index(db, int(row["id"]))
        count += 1
    return count


@dataclass(slots=True)
class SearchQuery:
    text: str | None = None
    genres: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    person_id: int | None = None
    years: tuple[int | None, int | None] = (None, None)
    content_type: str | None = None
    min_rating: float | None = None
    available_only: bool = True
    sort: str = "relevance"
    limit: int = 50
    offset: int = 0


def search_media(db: Database, q: SearchQuery) -> tuple[list[dict[str, Any]], int]:
    """FTS5 prefix search, or a filtered browse when no text is supplied.

    The FTS query is built with :func:`_fts_query`, which quotes every term --
    user text never reaches the FTS parser as syntax.
    """
    where = ["m.hidden = 0"]
    params: list[Any] = []
    order_by = "rank"
    fts_join = ""
    fts_where = ""
    fts_params: list[Any] = []

    if q.text and q.text.strip():
        match = _fts_query(q.text)
        if match:
            fts_join = "JOIN media_fts f ON f.rowid = m.id"
            fts_where = "AND media_fts MATCH ?"
            fts_params.append(match)

    if q.content_type:
        where.append("m.content_type = ?")
        params.append(q.content_type)
    if q.available_only:
        where.append("m.integrity_state != 'missing'")
    if q.min_rating is not None:
        where.append("m.rating_value >= ?")
        params.append(q.min_rating)
    if q.genres:
        placeholders = ",".join("?" * len(q.genres))
        where.append(
            f"m.id IN (SELECT mg.media_id FROM media_genres mg JOIN genres g "
            f"ON g.id = mg.genre_id WHERE g.slug IN ({placeholders}))"
        )
        params.extend(q.genres)
    if q.tags:
        placeholders = ",".join("?" * len(q.tags))
        where.append(
            f"m.id IN (SELECT mt.media_id FROM media_tags mt JOIN tags t "
            f"ON t.id = mt.tag_id WHERE t.slug IN ({placeholders}))"
        )
        params.extend(q.tags)
    if q.person_id:
        where.append("m.id IN (SELECT media_id FROM media_credits WHERE person_id = ?)")
        params.append(q.person_id)
    low, high = q.years
    if low is not None:
        where.append("m.year >= ?")
        params.append(low)
    if high is not None:
        where.append("m.year <= ?")
        params.append(high)

    if q.sort == "title":
        order_by = "m.sort_title ASC"
    elif q.sort == "year":
        order_by = "m.year DESC"
    elif q.sort == "rating":
        order_by = "m.rating_value DESC"
    elif q.sort == "added":
        order_by = "m.added_at DESC"
    elif fts_join:
        order_by = "bm25(media_fts, 8.0, 8.0, 2.0, 4.0, 3.0, 3.0, 1.5, 1.5)"
    else:
        order_by = "m.added_at DESC"

    base_where = " AND ".join(where)
    cols = ", ".join("m." + c.strip() for c in MEDIA_PUBLIC_COLUMNS.split(",") if c.strip())

    count_sql = (
        f"SELECT COUNT(*) FROM media m {fts_join} WHERE {base_where} {fts_where}"
    )
    total = int(db.scalar(count_sql, tuple(fts_params) + tuple(params)) or 0)

    sql = (
        f"SELECT {cols} FROM media m {fts_join} WHERE {base_where} {fts_where} "
        f"ORDER BY {order_by} LIMIT ? OFFSET ?"
    )
    all_params = tuple(fts_params) + tuple(params)
    rows = rows_to_dicts(
        db.query(sql, all_params + (max(1, min(q.limit, 200)), max(0, q.offset)))
    )
    return rows, total


_FTS_STRIP = str.maketrans({c: " " for c in '"\'`^~*?:.,;()[]{}!<>-_/\\|+=&%$#@'})


def _fts_query(text: str, *, max_terms: int = 12) -> str:
    """Turn free text into a safe FTS5 MATCH expression.

    Every term is double-quoted (escaping any embedded quote) and given a
    prefix wildcard, so "dune par" becomes ``"dune"* AND "par"*``.  Terms the
    tokenizer would choke on are stripped first.  Because nothing is emitted
    unquoted, a query like ``title:" OR 1=1 --`` is inert text, not syntax.
    """
    cleaned = (text or "").translate(_FTS_STRIP)
    terms = [t for t in cleaned.split() if t][:max_terms]
    if not terms:
        return ""
    parts = []
    for term in terms:
        escaped = term.replace('"', '""')
        parts.append(f'"{escaped}"*')
    return " AND ".join(parts)


# ---------------------------------------------------------------------------
# Favourites
# ---------------------------------------------------------------------------
def add_favorite(db: Database, profile_id: int, media_id: int) -> bool:
    """Returns True when the item was newly added."""
    existing = db.query_one(
        "SELECT 1 FROM favorites WHERE profile_id = ? AND media_id = ?", (profile_id, media_id)
    )
    if existing:
        return False
    db.execute(
        "INSERT INTO favorites (profile_id, media_id) VALUES (?, ?)", (profile_id, media_id)
    )
    return True


def remove_favorite(db: Database, profile_id: int, media_id: int) -> bool:
    result = db.execute(
        "DELETE FROM favorites WHERE profile_id = ? AND media_id = ?", (profile_id, media_id)
    )
    return result.changes > 0


def is_favorite(db: Database, profile_id: int, media_id: int) -> bool:
    return db.query_one(
        "SELECT 1 FROM favorites WHERE profile_id = ? AND media_id = ?", (profile_id, media_id)
    ) is not None


def favorite_media_ids(db: Database, profile_id: int) -> set[int]:
    return {
        int(row["media_id"])
        for row in db.query("SELECT media_id FROM favorites WHERE profile_id = ?", (profile_id,))
    }


def list_favorites(
    db: Database, profile_id: int, *, sort: str = "added", direction: str = "desc",
    limit: int = 100, offset: int = 0,
) -> list[dict[str, Any]]:
    order_by = SORT_COLUMNS.get(sort, "m.added_at")
    direction_sql = DIRECTIONS.get(direction.lower(), "DESC")
    if sort == "added":
        order_by = "f.created_at"
    cols = ", ".join("m." + c.strip() for c in MEDIA_PUBLIC_COLUMNS.split(",") if c.strip())
    rows = db.query(
        f"SELECT {cols} FROM favorites f JOIN media m ON m.id = f.media_id "
        f"WHERE f.profile_id = ? AND m.hidden = 0 "
        f"ORDER BY {order_by} IS NULL, {order_by} {direction_sql}, m.id {direction_sql} "
        f"LIMIT ? OFFSET ?",
        (profile_id, max(1, min(limit, 200)), max(0, offset)),
    )
    return rows_to_dicts(rows)


def count_favorites(db: Database, profile_id: int) -> int:
    return int(db.scalar("SELECT COUNT(*) FROM favorites WHERE profile_id = ?", (profile_id,)) or 0)


def favorites_with_progress(
    db: Database, profile_id: int, *, limit: int = 20
) -> list[dict[str, Any]]:
    """Home-screen favourites rail, with the viewer's own progress attached."""
    cols = ", ".join("m." + c.strip() for c in MEDIA_PUBLIC_COLUMNS.split(",") if c.strip())
    rows = db.query(
        f"SELECT {cols}, h.position_seconds, h.duration_seconds, h.percent, h.completed, "
        f"h.last_watched_at AS progress_last_watched "
        f"FROM favorites f JOIN media m ON m.id = f.media_id "
        f"LEFT JOIN watch_history h ON h.media_id = m.id AND h.profile_id = f.profile_id "
        f"WHERE f.profile_id = ? AND m.hidden = 0 "
        f"ORDER BY f.created_at DESC LIMIT ?",
        (profile_id, max(1, min(limit, 100))),
    )
    return rows_to_dicts(rows)


# ---------------------------------------------------------------------------
# Watch history
# ---------------------------------------------------------------------------
def upsert_progress(
    db: Database,
    profile_id: int,
    media_id: int,
    *,
    position_seconds: float,
    duration_seconds: float,
    completed: bool = False,
    percent: float | None = None,
) -> dict[str, Any]:
    computed = percent
    if computed is None:
        computed = (
            max(0.0, min(1.0, position_seconds / duration_seconds))
            if duration_seconds and duration_seconds > 0
            else 0.0
        )
    db.execute(
        """
        INSERT INTO watch_history (profile_id, media_id, position_seconds,
            duration_seconds, percent, completed, last_watched_at, finished_at)
        VALUES (?, ?, ?, ?, ?, ?, datetime('now'), ?)
        ON CONFLICT(profile_id, media_id) DO UPDATE SET
            position_seconds = excluded.position_seconds,
            duration_seconds = CASE
                WHEN excluded.duration_seconds > 0 THEN excluded.duration_seconds
                ELSE watch_history.duration_seconds END,
            percent = excluded.percent,
            completed = CASE
                WHEN excluded.completed = 1 THEN 1
                WHEN watch_history.completed = 1 AND excluded.duration_seconds > 0
                     AND excluded.position_seconds / excluded.duration_seconds < 0.05
                THEN 0
                ELSE watch_history.completed END,
            last_watched_at = datetime('now'),
            finished_at = CASE
                WHEN excluded.completed = 1 THEN datetime('now')
                WHEN watch_history.completed = 1 AND excluded.duration_seconds > 0
                     AND excluded.position_seconds / excluded.duration_seconds < 0.05
                THEN NULL
                ELSE watch_history.finished_at END,
            play_count = CASE
                WHEN watch_history.completed = 1
                     AND excluded.position_seconds < watch_history.position_seconds - 300
                THEN watch_history.play_count + 1
                ELSE watch_history.play_count END
        """,
        (profile_id, media_id, round(float(position_seconds), 2),
         round(float(duration_seconds), 2), round(float(computed), 4), int(completed),
         utcnow() if completed else None),
    )
    row = db.query_one(
        "SELECT * FROM watch_history WHERE profile_id = ? AND media_id = ?",
        (profile_id, media_id),
    )
    return dict(row) if row else {}


def get_progress(db: Database, profile_id: int, media_id: int) -> dict[str, Any] | None:
    return row_to_dict(
        db.query_one(
            "SELECT * FROM watch_history WHERE profile_id = ? AND media_id = ?",
            (profile_id, media_id),
        )
    )


def delete_progress(db: Database, profile_id: int, media_id: int) -> bool:
    result = db.execute(
        "DELETE FROM watch_history WHERE profile_id = ? AND media_id = ?",
        (profile_id, media_id),
    )
    return result.changes > 0


def clear_history(db: Database, profile_id: int, *, before: str | None = None) -> int:
    if before:
        result = db.execute(
            "DELETE FROM watch_history WHERE profile_id = ? AND last_watched_at < ?",
            (profile_id, before),
        )
    else:
        result = db.execute("DELETE FROM watch_history WHERE profile_id = ?", (profile_id,))
    return result.changes


def history_entries(
    db: Database, profile_id: int, *, limit: int = 50, offset: int = 0,
    completed: bool | None = None,
) -> list[dict[str, Any]]:
    where = ["h.profile_id = ?", "m.hidden = 0"]
    params: list[Any] = [profile_id]
    if completed is not None:
        where.append("h.completed = ?")
        params.append(int(completed))
    cols = ", ".join("m." + c.strip() for c in MEDIA_PUBLIC_COLUMNS.split(",") if c.strip())
    rows = db.query(
        f"SELECT {cols}, h.position_seconds, h.duration_seconds, h.percent, h.completed, "
        f"h.first_watched_at, h.last_watched_at, h.play_count, h.finished_at "
        f"FROM watch_history h JOIN media m ON m.id = h.media_id "
        f"WHERE {' AND '.join(where)} "
        f"ORDER BY h.last_watched_at DESC LIMIT ? OFFSET ?",
        tuple(params) + (max(1, min(limit, 200)), max(0, offset)),
    )
    return rows_to_dicts(rows)


def continue_watching(
    db: Database, profile_id: int, *, limit: int = 20,
    completion_threshold: float = 0.97,
) -> list[dict[str, Any]]:
    cols = ", ".join("m." + c.strip() for c in MEDIA_PUBLIC_COLUMNS.split(",") if c.strip())
    rows = db.query(
        f"SELECT {cols}, h.position_seconds, h.duration_seconds, h.percent, h.completed, "
        f"h.last_watched_at AS progress_last_watched "
        f"FROM watch_history h JOIN media m ON m.id = h.media_id "
        f"WHERE h.profile_id = ? AND m.hidden = 0 AND m.integrity_state != 'missing' "
        f"  AND h.completed = 0 AND h.percent > 0.01 AND h.percent < ? "
        f"ORDER BY h.last_watched_at DESC LIMIT ?",
        (profile_id, completion_threshold, max(1, min(limit, 100))),
    )
    return rows_to_dicts(rows)


def count_history(db: Database, profile_id: int) -> int:
    return int(db.scalar("SELECT COUNT(*) FROM watch_history WHERE profile_id = ?", (profile_id,)) or 0)


# ---------------------------------------------------------------------------
# Ratings
# ---------------------------------------------------------------------------
def set_rating(db: Database, profile_id: int, media_id: int, score: float) -> dict[str, Any]:
    db.execute(
        "INSERT INTO ratings (profile_id, media_id, score) VALUES (?, ?, ?) "
        "ON CONFLICT(profile_id, media_id) DO UPDATE SET "
        "score = excluded.score, updated_at = datetime('now')",
        (profile_id, media_id, float(score)),
    )
    return row_to_dict(
        db.query_one(
            "SELECT * FROM ratings WHERE profile_id = ? AND media_id = ?",
            (profile_id, media_id),
        )
    ) or {}


def delete_rating(db: Database, profile_id: int, media_id: int) -> bool:
    result = db.execute(
        "DELETE FROM ratings WHERE profile_id = ? AND media_id = ?", (profile_id, media_id)
    )
    return result.changes > 0


def get_rating(db: Database, profile_id: int, media_id: int) -> float | None:
    value = db.scalar(
        "SELECT score FROM ratings WHERE profile_id = ? AND media_id = ?",
        (profile_id, media_id),
    )
    return float(value) if value is not None else None


def ratings_map(db: Database, profile_id: int, media_ids: Sequence[int]) -> dict[int, float]:
    if not media_ids:
        return {}
    placeholders = ",".join("?" * len(media_ids))
    return {
        int(row["media_id"]): float(row["score"])
        for row in db.query(
            f"SELECT media_id, score FROM ratings WHERE profile_id = ? "
            f"AND media_id IN ({placeholders})",
            (profile_id, *media_ids),
        )
    }


# ---------------------------------------------------------------------------
# Categories / facets
# ---------------------------------------------------------------------------
def list_genres_with_counts(db: Database, *, content_type: str | None = None,
                            limit: int = 200) -> list[dict[str, Any]]:
    params: list[Any] = []
    join_clause = ""
    if content_type:
        join_clause = " AND m.content_type = ?"
        params.append(content_type)
    return rows_to_dicts(
        db.query(
            "SELECT g.id, g.name, g.slug, COUNT(mg.media_id) AS count "
            "FROM genres g "
            "LEFT JOIN media_genres mg ON mg.genre_id = g.id "
            "LEFT JOIN media m ON m.id = mg.media_id AND m.hidden = 0" + join_clause + " "
            "GROUP BY g.id HAVING count > 0 ORDER BY count DESC, g.name LIMIT ?",
            tuple(params) + (max(1, limit),),
        )
    )


def list_years_with_counts(db: Database, *, limit: int = 100) -> list[dict[str, Any]]:
    return rows_to_dicts(
        db.query(
            "SELECT year, COUNT(*) AS count FROM media "
            "WHERE hidden = 0 AND year IS NOT NULL "
            "GROUP BY year ORDER BY year DESC LIMIT ?",
            (max(1, limit),),
        )
    )


def list_decades_with_counts(db: Database) -> list[dict[str, Any]]:
    return rows_to_dicts(
        db.query(
            "SELECT CAST(year / 10 * 10 AS INTEGER) AS decade, COUNT(*) AS count "
            "FROM media WHERE hidden = 0 AND year IS NOT NULL "
            "GROUP BY decade ORDER BY decade DESC"
        )
    )


def list_tags_with_counts(db: Database, *, limit: int = 100) -> list[dict[str, Any]]:
    return rows_to_dicts(
        db.query(
            "SELECT t.id, t.name, t.slug, COUNT(mt.media_id) AS count "
            "FROM tags t "
            "LEFT JOIN media_tags mt ON mt.tag_id = t.id "
            "LEFT JOIN media m ON m.id = mt.media_id AND m.hidden = 0 "
            "GROUP BY t.id HAVING count > 0 ORDER BY count DESC, t.name LIMIT ?",
            (max(1, limit),),
        )
    )


def list_collections(db: Database) -> list[dict[str, Any]]:
    return rows_to_dicts(
        db.query(
            "SELECT c.*, COUNT(cm.media_id) AS count FROM collections c "
            "LEFT JOIN collection_media cm ON cm.collection_id = c.id "
            "GROUP BY c.id ORDER BY c.name"
        )
    )


def list_people(db: Database, *, limit: int = 100, search: str | None = None) -> list[dict[str, Any]]:
    where, params = [], []
    if search:
        where.append("name LIKE ?")
        params.append(f"%{search}%")
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    return rows_to_dicts(
        db.query(
            f"SELECT p.id, p.name, p.slug, p.role, p.photo_key, "
            f"(SELECT COUNT(*) FROM media_credits mc WHERE mc.person_id = p.id) AS credit_count "
            f"FROM people p {clause} ORDER BY credit_count DESC, p.name LIMIT ?",
            tuple(params) + (max(1, min(limit, 500)),),
        )
    )


def library_stats(db: Database) -> dict[str, Any]:
    row = db.query_one(
        """
        SELECT COUNT(*) AS total,
               SUM(CASE WHEN content_type = 'movie' THEN 1 ELSE 0 END) AS movies,
               SUM(CASE WHEN content_type = 'series' THEN 1 ELSE 0 END) AS series,
               SUM(CASE WHEN content_type = 'episode' THEN 1 ELSE 0 END) AS episodes,
               SUM(CASE WHEN integrity_state = 'missing' THEN 1 ELSE 0 END) AS missing,
               SUM(CASE WHEN hidden = 1 THEN 1 ELSE 0 END) AS hidden,
               COALESCE(SUM(file_size_bytes), 0) AS total_bytes,
               COALESCE(SUM(runtime_minutes), 0) AS total_runtime,
               MIN(year) AS oldest_year,
               MAX(year) AS newest_year,
               MIN(added_at) AS first_added,
               MAX(added_at) AS last_added
          FROM media WHERE hidden = 0
        """
    )
    stats = dict(row) if row else {}
    stats["total_bytes"] = int(stats.get("total_bytes") or 0)
    stats["total_runtime"] = int(stats.get("total_runtime") or 0)
    return stats


# ---------------------------------------------------------------------------
# Collections
# ---------------------------------------------------------------------------
def create_collection(db: Database, name: str, slug: str, description: str = "",
                      poster_key: str | None = None) -> int:
    result = db.execute(
        "INSERT INTO collections (name, slug, description, poster_key) VALUES (?, ?, ?, ?)",
        (name, slug, description, poster_key),
    )
    return result.rowid


def add_to_collection(db: Database, collection_id: int, media_id: int, position: int = 0) -> None:
    db.execute(
        "INSERT OR IGNORE INTO collection_media (collection_id, media_id, position) "
        "VALUES (?, ?, ?)",
        (collection_id, media_id, position),
    )


def collection_media(db: Database, collection_id: int, *, limit: int = 200) -> list[dict[str, Any]]:
    cols = ", ".join("m." + c.strip() for c in MEDIA_PUBLIC_COLUMNS.split(",") if c.strip())
    return rows_to_dicts(
        db.query(
            f"SELECT {cols} FROM collection_media cm JOIN media m ON m.id = cm.media_id "
            f"WHERE cm.collection_id = ? AND m.hidden = 0 "
            f"ORDER BY cm.position, m.sort_title LIMIT ?",
            (collection_id, max(1, min(limit, 500))),
        )
    )


# ---------------------------------------------------------------------------
# Playback sessions
# ---------------------------------------------------------------------------
def create_playback_session(
    db: Database, *, session_id: str, profile_id: int, media_id: int, user_id: int,
    token_hash: str, expires_at: str, start_offset: float = 0.0, client_ip: str | None = None,
) -> dict[str, Any]:
    db.execute(
        "INSERT INTO playback_sessions (id, profile_id, media_id, user_id, token_hash, "
        "expires_at, start_offset, client_ip) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (session_id, profile_id, media_id, user_id, token_hash, expires_at,
         float(start_offset), client_ip),
    )
    row = db.query_one("SELECT * FROM playback_sessions WHERE id = ?", (session_id,))
    return dict(row) if row else {}


def touch_playback_session(db: Database, session_id: str, *, bytes_served: int = 0) -> None:
    db.execute(
        "UPDATE playback_sessions SET last_seen_at = datetime('now'), requests = requests + 1, "
        "bytes_served = bytes_served + ? WHERE id = ?",
        (max(0, bytes_served), session_id),
    )


def end_playback_session(db: Database, session_id: str) -> None:
    db.execute(
        "UPDATE playback_sessions SET ended = 1, last_seen_at = datetime('now') WHERE id = ?",
        (session_id,),
    )


def get_playback_session(db: Database, session_id: str) -> dict[str, Any] | None:
    return row_to_dict(
        db.query_one("SELECT * FROM playback_sessions WHERE id = ? AND ended = 0", (session_id,))
    )


def prune_playback_sessions(db: Database, keep_hours: int = 6) -> int:
    result = db.execute(
        "DELETE FROM playback_sessions WHERE last_seen_at < datetime('now', ?)",
        (f"-{max(1, keep_hours)} hours",),
    )
    return result.changes


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------
def get_setting(db: Database, key: str, default: str | None = None) -> str | None:
    value = db.scalar("SELECT value FROM settings WHERE key = ?", (key,))
    return value if value is not None else default


def set_setting(db: Database, key: str, value: str) -> None:
    db.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
        "updated_at = datetime('now')",
        (key, str(value)),
    )


def get_json_setting(db: Database, key: str, fallback: Any) -> Any:
    raw = get_setting(db, key)
    return _json_field(raw, fallback)


def set_json_setting(db: Database, key: str, value: Any) -> None:
    set_setting(db, key, json.dumps(value, separators=(",", ":"), ensure_ascii=False))


# ---------------------------------------------------------------------------
# Recommendations (rebuilt on demand)
# ---------------------------------------------------------------------------
def save_recommendations(db: Database, profile_id: int,
                         scored: Sequence[tuple[int, float, str]]) -> int:
    with db.write() as conn:
        conn.execute("DELETE FROM recommendations WHERE profile_id = ?", (profile_id,))
        conn.executemany(
            "INSERT OR REPLACE INTO recommendations (profile_id, media_id, score, reason, "
            "computed_at) VALUES (?, ?, ?, ?, datetime('now'))",
            [(profile_id, media_id, score, reason) for media_id, score, reason in scored],
        )
    return len(scored)


def list_recommendations(db: Database, profile_id: int, *, limit: int = 20,
                         exclude: Sequence[int] = ()) -> list[dict[str, Any]]:
    cols = ", ".join("m." + c.strip() for c in MEDIA_PUBLIC_COLUMNS.split(",") if c.strip())
    params: list[Any] = [profile_id]
    exclude_clause = ""
    if exclude:
        placeholders = ",".join("?" * len(exclude))
        exclude_clause = f"AND m.id NOT IN ({placeholders})"
        params.extend(exclude)
    rows = db.query(
        f"SELECT {cols}, r.score AS recommendation_score, r.reason AS recommendation_reason "
        f"FROM recommendations r JOIN media m ON m.id = r.media_id "
        f"WHERE r.profile_id = ? AND m.hidden = 0 AND m.integrity_state != 'missing' "
        f"{exclude_clause} ORDER BY r.score DESC LIMIT ?",
        tuple(params) + (max(1, min(limit, 100)),),
    )
    return rows_to_dicts(rows)


def clear_recommendations(db: Database, profile_id: int) -> None:
    db.execute("DELETE FROM recommendations WHERE profile_id = ?", (profile_id,))


def set_integrity_state(db: Database, media_id: int, state: str) -> None:
    db.execute(
        "UPDATE media SET integrity_state = ?, availability = ? WHERE id = ?",
        (state, state if state in {"ok", "missing", "corrupt"} else "unknown", media_id),
    )


def media_needing_integrity_check(db: Database) -> list[dict[str, Any]]:
    return rows_to_dicts(
        db.query(
            "SELECT id, media_path, integrity_state FROM media "
            "WHERE integrity_state != 'ok' OR integrity_state IS NULL"
        )
    )


from dataclasses import field  # noqa: E402  (kept last so dataclass defaults read cleanly)