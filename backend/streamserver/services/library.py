"""Library browsing, search, categories and per-profile favourites.

Everything here is *paged*.  The client never receives the whole table: a
1000-title library answers any rail in a single indexed query, and a search
caps out at 200 rows per page.
"""

from __future__ import annotations

import logging
from typing import Any, Iterable

from ..config import MediaConfig
from ..db import repositories as repo
from ..db.database import Database
from ..db.repositories import MediaFilter, SearchQuery

log = logging.getLogger(__name__)

MAX_PAGE_SIZE = 200
DEFAULT_PAGE_SIZE = 50

SORTABLE = set(repo.SORT_COLUMNS)
SEARCH_SORTS = {"relevance", "title", "year", "rating", "added"}
GROUPINGS = set(repo.GROUP_BY)


class LibraryError(Exception):
    def __init__(self, message: str, code: str = "invalid_filter",
                 status_code: int = 422) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


class LibraryService:
    def __init__(self, db: Database, config: MediaConfig) -> None:
        self.db = db
        self.config = config

    # -- browsing ---------------------------------------------------------
    def browse(self, profile_id: int, **kwargs: Any) -> dict[str, Any]:
        f = self._filter(kwargs)
        total = repo.count_media(self.db, f)
        items = repo.list_media(self.db, f)
        items = repo.attach_relations(self.db, items)
        self._attach_profile_state(profile_id, items)
        return self._page(items, total, f.offset, f.limit)

    def get(self, profile_id: int, media_id: int) -> dict[str, Any]:
        item = repo.get_media(self.db, media_id)
        if not item or item.get("hidden"):
            raise LibraryError("Media not found.", code="media_not_found", status_code=404)
        item = repo.attach_relations_one(self.db, item)
        self._attach_profile_state(profile_id, [item])
        item["streams"] = repo.get_media_streams(self.db, media_id)
        return item

    def get_by_slug(self, profile_id: int, slug: str) -> dict[str, Any]:
        row = repo.get_media_by_slug(self.db, slug)
        if not row:
            raise LibraryError("Media not found.", code="media_not_found", status_code=404)
        return self.get(profile_id, int(row["id"]))

    # -- search -----------------------------------------------------------
    def search(self, profile_id: int, **kwargs: Any) -> dict[str, Any]:
        q = self._search_query(kwargs)
        rows, total = repo.search_media(self.db, q)
        rows = repo.attach_relations(self.db, rows)
        self._attach_profile_state(profile_id, rows)
        return self._page(rows, total, q.offset, q.limit)

    def suggest(self, profile_id: int, prefix: str, *, limit: int = 12) -> list[dict[str, Any]]:
        rows, _ = repo.search_media(
            self.db,
            SearchQuery(text=prefix, limit=max(1, min(limit, 25))),
        )
        self._attach_profile_state(profile_id, rows)
        return [
            {
                "id": row["id"],
                "slug": row["slug"],
                "title": row["title"],
                "year": row.get("year"),
                "poster_key": row.get("poster_key"),
                "content_type": row.get("content_type"),
            }
            for row in rows
        ]

    # -- facets -----------------------------------------------------------
    def categories(self) -> dict[str, Any]:
        return self.facets()

    def stats(self) -> dict[str, Any]:
        return repo.library_stats(self.db)

    def facets(self) -> dict[str, Any]:
        """Genres, years, tags and totals for the categories screen.

        Every list here is materialised into plain dicts: the underlying
        queries return ``sqlite3.Row`` objects, which FastAPI cannot serialise.
        """
        db = self.db
        return {
            "genres": [dict(row) for row in repo.list_genres_with_counts(db)],
            "years": [dict(row) for row in repo.list_years_with_counts(db)],
            "decades": [dict(row) for row in repo.list_decades_with_counts(db)],
            "tags": [dict(row) for row in repo.list_tags_with_counts(db, limit=60)],
            "collections": [dict(row) for row in repo.list_collections(db)],
            "content_types": [
                dict(row) for row in db.query(
                    "SELECT content_type, COUNT(*) AS count FROM media "
                    "WHERE hidden = 0 GROUP BY content_type"
                )
            ],
            "stats": repo.library_stats(db),
        }

    def collection(self, collection_id: int) -> dict[str, Any]:
        row = self.db.query_one("SELECT * FROM collections WHERE id = ?", (collection_id,))
        if not row:
            raise LibraryError("Collection not found.", code="collection_not_found", status_code=404)
        items = repo.collection_media(self.db, collection_id)
        return {**dict(row), "items": repo.attach_relations(self.db, items)}

    def people(self, *, search: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        return [dict(row) for row in repo.list_people(self.db, search=search, limit=limit)]

    def person(self, person_id: int) -> dict[str, Any]:
        row = self.db.query_one("SELECT * FROM people WHERE id = ?", (person_id,))
        if not row:
            raise LibraryError("Person not found.", code="person_not_found", status_code=404)
        credits = self.db.query(
            "SELECT m.* FROM media_credits mc JOIN media m ON m.id = mc.media_id "
            "WHERE mc.person_id = ? AND m.hidden = 0 ORDER BY m.year DESC",
            (person_id,),
        )
        return {**dict(row), "credits": repo.attach_relations(self.db, [dict(r) for r in credits])}

    # -- favourites -------------------------------------------------------
    def favorites(self, profile_id: int, *, sort: str = "added",
                  direction: str = "desc", limit: int = DEFAULT_PAGE_SIZE,
                  offset: int = 0) -> dict[str, Any]:
        items = repo.list_favorites(
            self.db, profile_id, sort=sort, direction=direction, limit=limit, offset=offset
        )
        total = repo.count_favorites(self.db, profile_id)
        self._attach_profile_state(profile_id, items)
        return self._page(items, total, offset, limit)

    def set_favorite(self, profile_id: int, media_id: int, favorite: bool) -> bool:
        if not repo.get_media(self.db, media_id):
            raise LibraryError("Media not found.", code="media_not_found", status_code=404)
        if favorite:
            return repo.add_favorite(self.db, profile_id, media_id)
        return repo.remove_favorite(self.db, profile_id, media_id)

    def is_favorite(self, profile_id: int, media_id: int) -> bool:
        return repo.is_favorite(self.db, profile_id, media_id)

    # -- home rails -------------------------------------------------------
    def home_rails(self, profile_id: int, *, limit: int = 20) -> dict[str, Any]:
        """Everything the home screen needs, in one pass of cheap indexed queries."""
        rails: dict[str, Any] = {}

        rails["continue_watching"] = self._rail(
            repo.continue_watching(
                self.db, profile_id, limit=limit,
                completion_threshold=self.config.completion_threshold,
            ), profile_id,
        )
        rails["recently_added"] = self._rail(
            repo.list_media(self.db, MediaFilter(sort="added", direction="desc", limit=limit)),
            profile_id,
        )
        rails["recommended"] = self._rail(
            repo.list_recommendations(self.db, profile_id, limit=limit), profile_id
        )
        rails["popular"] = self._rail(
            repo.list_media(self.db, MediaFilter(sort="popularity", direction="desc", limit=limit)),
            profile_id,
        )
        rails["top_rated"] = self._rail(
            repo.list_media(self.db, MediaFilter(sort="rating", direction="desc", limit=limit)),
            profile_id,
        )
        rails["favorites"] = self._rail(
            repo.favorites_with_progress(self.db, profile_id, limit=limit), profile_id
        )
        rails["recently_watched"] = self._rail(
            repo.history_entries(self.db, profile_id, limit=limit), profile_id
        )

        genres = repo.list_genres_with_counts(self.db, limit=12)
        rails["categories"] = [
            {
                "name": g["name"],
                "slug": g["slug"],
                "count": g["count"],
                "poster_key": _pick_poster(self.db, g["slug"]),
            }
            for g in genres
        ]
        return rails

    # -- helpers ----------------------------------------------------------
    def _rail(self, rows: Iterable[dict[str, Any]], profile_id: int) -> list[dict[str, Any]]:
        items = [dict(row) for row in rows]
        self._attach_profile_state(profile_id, items)
        return items

    def _attach_profile_state(self, profile_id: int, items: list[dict[str, Any]]) -> None:
        """Attach favourite flag and the viewer's rating in two queries."""
        if not items:
            return
        ids = [int(item["id"]) for item in items]
        favorites = repo.favorite_media_ids(self.db, profile_id)
        ratings = repo.ratings_map(self.db, profile_id, ids)
        for item in items:
            media_id = int(item["id"])
            item["is_favorite"] = media_id in favorites
            item["user_rating"] = ratings.get(media_id)

    def _page(self, items: list[dict[str, Any]], total: int, offset: int,
              limit: int) -> dict[str, Any]:
        return {
            "items": items,
            "total": total,
            "offset": offset,
            "limit": limit,
            "has_more": offset + len(items) < total,
        }

    def _filter(self, kwargs: dict[str, Any]) -> MediaFilter:
        sort = str(kwargs.get("sort") or "added").lower()
        if sort not in SORTABLE:
            raise LibraryError(
                f"Unsupported sort {sort!r}. Valid values: {', '.join(sorted(SORTABLE))}."
            )
        direction = str(kwargs.get("direction") or ("asc" if sort == "title" else "desc")).lower()
        if direction not in repo.DIRECTIONS:
            raise LibraryError("direction must be 'asc' or 'desc'.")

        years = _parse_years(kwargs.get("year"), kwargs.get("year_from"), kwargs.get("year_to"))
        return MediaFilter(
            content_type=_validate_content_type(kwargs.get("content_type")),
            genres=_as_slug_list(kwargs.get("genre")),
            tags=_as_slug_list(kwargs.get("tag")),
            years=years,
            decade=_parse_int(kwargs.get("decade")),
            min_rating=_parse_float(kwargs.get("min_rating")),
            max_rating=_parse_float(kwargs.get("max_rating")),
            min_runtime=_parse_int(kwargs.get("min_runtime")),
            max_runtime=_parse_int(kwargs.get("max_runtime")),
            resolution=_validate_resolution(kwargs.get("resolution")),
            hdr_only=_parse_bool(kwargs.get("hdr")),
            parental_rating=_clean_str(kwargs.get("parental_rating")),
            person_id=_parse_int(kwargs.get("person_id")),
            collection_id=_parse_int(kwargs.get("collection_id")),
            added_after=_clean_str(kwargs.get("added_after")),
            added_before=_clean_str(kwargs.get("added_before")),
            available_only=not _parse_bool(kwargs.get("include_missing")),
            hide_missing=_parse_bool(kwargs.get("only_available")),
            sort=sort,
            direction=direction,
            limit=_clamp(kwargs.get("limit"), DEFAULT_PAGE_SIZE, 1, MAX_PAGE_SIZE),
            offset=_clamp(kwargs.get("offset"), 0, 0, 1_000_000),
        )

    def _search_query(self, kwargs: dict[str, Any]) -> SearchQuery:
        sort = str(kwargs.get("sort") or "relevance").lower()
        if sort not in SEARCH_SORTS:
            raise LibraryError(
                f"Unsupported search sort {sort!r}. "
                f"Valid values: {', '.join(sorted(SEARCH_SORTS))}."
            )
        text = _clean_str(kwargs.get("q")) or ""
        if len(text) > 200:
            raise LibraryError("Search query is too long.", code="query_too_long")
        return SearchQuery(
            text=text,
            genres=_as_slug_list(kwargs.get("genre")),
            tags=_as_slug_list(kwargs.get("tag")),
            person_id=_parse_int(kwargs.get("person_id")),
            years=_parse_years(kwargs.get("year"), kwargs.get("year_from"), kwargs.get("year_to")),
            content_type=_validate_content_type(kwargs.get("content_type")),
            min_rating=_parse_float(kwargs.get("min_rating")),
            available_only=not _parse_bool(kwargs.get("include_missing")),
            sort=sort,
            limit=_clamp(kwargs.get("limit"), DEFAULT_PAGE_SIZE, 1, MAX_PAGE_SIZE),
            offset=_clamp(kwargs.get("offset"), 0, 0, 1_000_000),
        )


def _pick_poster(db: Database, genre_slug: str) -> str | None:
    row = db.query_one(
        "SELECT m.poster_key FROM media_genres mg JOIN media m ON m.id = mg.media_id "
        "JOIN genres g ON g.id = mg.genre_id "
        "WHERE g.slug = ? AND m.poster_key IS NOT NULL AND m.hidden = 0 "
        "ORDER BY m.rating_value DESC, m.added_at DESC LIMIT 1",
        (genre_slug,),
    )
    return row[0] if row else None


def _clean_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text or len(text) > 200:
        return None
    # Strip control characters so a crafted value cannot corrupt log lines.
    return "".join(ch for ch in text if ch.isprintable())


def _as_slug_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        raw = list(value)
    else:
        raw = str(value).split(",")
    result = []
    for item in raw:
        text = str(item).strip().lower()
        if text and len(text) <= 64 and text not in result:
            result.append(text)
    return result[:20]


def _parse_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise LibraryError(f"Expected an integer, got {value!r}.") from exc
    return parsed


def _parse_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise LibraryError(f"Expected a number, got {value!r}.") from exc
    if parsed != parsed or parsed in (float("inf"), float("-inf")):
        raise LibraryError("Numeric filters must be finite.")
    return parsed


def _parse_bool(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _parse_years(year: Any, year_from: Any, year_to: Any) -> tuple[int | None, int | None]:
    low = _parse_int(year_from) if year_from not in (None, "") else None
    high = _parse_int(year_to) if year_to not in (None, "") else None
    if year not in (None, ""):
        exact = _parse_int(year)
        if exact is None:
            raise LibraryError(f"Expected a year, got {year!r}.")
        low = high = exact
    if low is not None and high is not None and low > high:
        raise LibraryError("year_from cannot be greater than year_to.")
    for bound in (low, high):
        if bound is not None and not (1888 <= bound <= 2200):
            raise LibraryError("Years must be between 1888 and 2200.")
    return low, high


def _validate_content_type(value: Any) -> str | None:
    if value in (None, ""):
        return None
    text = str(value).lower()
    if text not in repo.CONTENT_TYPES:
        raise LibraryError(
            f"content_type must be one of: {', '.join(repo.CONTENT_TYPES)}."
        )
    return text


def _validate_resolution(value: Any) -> str | None:
    if value in (None, ""):
        return None
    text = str(value).lower()
    if text not in {"4k", "2160p", "1440p", "1080p", "720p", "sd"}:
        raise LibraryError(
            "resolution must be one of: 4k, 2160p, 1440p, 1080p, 720p, sd."
        )
    return text


def _clamp(value: Any, default: int, low: int, high: int) -> int:
    if value in (None, ""):
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(parsed, high))