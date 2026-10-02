"""Library routes: browse, search, categories, favourites, history, ratings.

Every route here is profile-scoped: ``profile_id`` comes from the dependency,
never from the query string, so a caller cannot read another profile's history
by appending a parameter.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Query, Request

from ..deps import CallerDep, Ctx, ProfileDep, _enforce_limit
from ..schemas import (
    MediaPathId,
    MediaOut,
    MediaPage,
    OkResponse,
    ProgressOut,
    ProgressUpdate,
    RatingOut,
    RatingUpdate,
)

router = APIRouter(prefix="/library", tags=["library"])


def _page_kwargs(
    limit: int | None, offset: int,
    sort: str | None, direction: str | None,
    content_type: str | None, genre: str | None, tag: str | None,
    year: int | None, year_from: int | None, year_to: int | None,
    decade: int | None, min_rating: float | None, max_rating: float | None,
    min_runtime: int | None, max_runtime: int | None, resolution: str | None,
    hdr: bool | None, parental_rating: str | None, person_id: int | None,
    collection_id: int | None, include_missing: bool | None,
) -> dict[str, Any]:
    """Collect the filter parameters the library service understands.

    ``None`` is meaningful throughout: it means "the client did not ask", and
    the service applies its own default.  Dropping unset keys here keeps the
    service signature honest.
    """
    return {
        "limit": limit, "offset": offset, "sort": sort, "direction": direction,
        "content_type": content_type, "genre": genre, "tag": tag,
        "year": year, "year_from": year_from, "year_to": year_to,
        "decade": decade, "min_rating": min_rating, "max_rating": max_rating,
        "min_runtime": min_runtime, "max_runtime": max_runtime,
        "resolution": resolution, "hdr": hdr, "parental_rating": parental_rating,
        "person_id": person_id, "collection_id": collection_id,
        "include_missing": include_missing,
    }


@router.get("/media", response_model=MediaPage, summary="Browse the library")
async def list_media(
    context: Ctx, profile_id: ProfileDep,
    limit: Annotated[int | None, Query(ge=1, le=200)] = None,
    offset: Annotated[int, Query(ge=0)] = 0,
    sort: str | None = None,
    direction: str | None = None,
    content_type: str | None = None,
    genre: str | None = None,
    tag: str | None = None,
    year: Annotated[int | None, Query(ge=1888, le=2200)] = None,
    year_from: Annotated[int | None, Query(ge=1888, le=2200)] = None,
    year_to: Annotated[int | None, Query(ge=1888, le=2200)] = None,
    decade: Annotated[int | None, Query(ge=1880, le=2200)] = None,
    min_rating: Annotated[float | None, Query(ge=0, le=10)] = None,
    max_rating: Annotated[float | None, Query(ge=0, le=10)] = None,
    min_runtime: Annotated[int | None, Query(ge=0, le=1000)] = None,
    max_runtime: Annotated[int | None, Query(ge=0, le=1000)] = None,
    resolution: str | None = None,
    hdr: bool | None = None,
    parental_rating: str | None = None,
    person_id: int | None = None,
    collection_id: int | None = None,
    include_missing: bool | None = None,
) -> MediaPage:
    result = context.library.browse(
        profile_id, **_page_kwargs(
            limit, offset, sort, direction, content_type, genre, tag, year,
            year_from, year_to, decade, min_rating, max_rating, min_runtime,
            max_runtime, resolution, hdr, parental_rating, person_id,
            collection_id, include_missing,
        )
    )
    return MediaPage(**result)


@router.get("/media/{media_id}", response_model=MediaOut,
            summary="One title with full metadata")
async def get_media(media_id: MediaPathId, context: Ctx, profile_id: ProfileDep) -> MediaOut:
    item = context.library.get(profile_id, media_id)
    return MediaOut(**item)


@router.get("/media/by-slug/{slug}", response_model=MediaOut,
            summary="One title addressed by its stable slug")
async def get_media_by_slug(slug: str, context: Ctx, profile_id: ProfileDep) -> MediaOut:
    item = context.library.get_by_slug(profile_id, slug)
    return MediaOut(**item)


@router.get("/search", response_model=MediaPage, summary="Full-text search")
async def search(
    request: Request, context: Ctx, profile_id: ProfileDep,
    q: Annotated[str | None, Query(max_length=200)] = None,
    limit: Annotated[int | None, Query(ge=1, le=200)] = None,
    offset: Annotated[int, Query(ge=0)] = 0,
    sort: str | None = None,
    genre: str | None = None,
    tag: str | None = None,
    year: Annotated[int | None, Query(ge=1888, le=2200)] = None,
    year_from: Annotated[int | None, Query(ge=1888, le=2200)] = None,
    year_to: Annotated[int | None, Query(ge=1888, le=2200)] = None,
    content_type: str | None = None,
    person_id: int | None = None,
    min_rating: Annotated[float | None, Query(ge=0, le=10)] = None,
    include_missing: bool | None = None,
) -> MediaPage:
    # Search is the most expensive query in the app (FTS join), so it gets its
    # own, tighter bucket than the general API one.
    _enforce_limit(context, "search", f"user:{profile_id}", request=request)

    result = context.library.search(
        profile_id,
        q=q, limit=limit, offset=offset, sort=sort, genre=genre, tag=tag,
        year=year, year_from=year_from, year_to=year_to,
        content_type=content_type, person_id=person_id, min_rating=min_rating,
        include_missing=include_missing,
    )
    return MediaPage(**result)


@router.get("/suggest", response_model=list[dict], summary="Type-ahead suggestions")
async def suggest(
    request: Request, context: Ctx, profile_id: ProfileDep,
    q: Annotated[str, Query(min_length=1, max_length=100)],
    limit: Annotated[int, Query(ge=1, le=25)] = 10,
) -> list[dict]:
    _enforce_limit(context, "search", f"user:{profile_id}", request=request)
    return context.library.suggest(profile_id, q, limit=limit)


@router.get("/categories", summary="Genres, years, tags and library statistics")
async def categories(context: Ctx, caller: CallerDep) -> dict:
    return context.library.categories()


@router.get("/stats", summary="Library totals")
async def stats(context: Ctx, caller: CallerDep) -> dict:
    return context.library.stats()


@router.get("/collections/{collection_id}", summary="One collection")
async def collection(collection_id: int, context: Ctx, caller: CallerDep) -> dict:
    return context.library.collection(collection_id)


@router.get("/people", summary="Cast and crew index")
async def people(
    context: Ctx, caller: CallerDep,
    search: Annotated[str | None, Query(max_length=80)] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[dict]:
    return context.library.people(search=search, limit=limit)


@router.get("/people/{person_id}", summary="One person's credits")
async def person(person_id: int, context: Ctx, caller: CallerDep) -> dict:
    return context.library.person(person_id)


@router.get("/home", summary="Every rail the home screen needs")
async def home(
    request: Request, context: Ctx, profile_id: ProfileDep,
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
) -> dict:
    return context.library.home_rails(profile_id, limit=limit)


# ---------------------------------------------------------------------------
# Favourites
# ---------------------------------------------------------------------------
@router.get("/favorites", response_model=MediaPage, summary="This profile's favourites")
async def favorites(
    context: Ctx, profile_id: ProfileDep,
    sort: str | None = None, direction: str | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> MediaPage:
    result = context.library.favorites(
        profile_id, sort=sort or "added", direction=direction or "desc",
        limit=limit, offset=offset,
    )
    return MediaPage(**result)


@router.put("/favorites/{media_id}", response_model=OkResponse,
            summary="Add a title to this profile's favourites")
async def add_favorite(media_id: MediaPathId, context: Ctx, profile_id: ProfileDep) -> OkResponse:
    added = context.library.set_favorite(profile_id, media_id, True)
    return OkResponse(detail="Added to favourites." if added else "Already a favourite.")


@router.delete("/favorites/{media_id}", response_model=OkResponse,
               summary="Remove a title from this profile's favourites")
async def remove_favorite(media_id: MediaPathId, context: Ctx, profile_id: ProfileDep) -> OkResponse:
    removed = context.library.set_favorite(profile_id, media_id, False)
    return OkResponse(detail="Removed from favourites." if removed else "Not a favourite.")


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------
@router.get("/history", response_model=MediaPage, summary="Watch history")
async def history(
    context: Ctx, profile_id: ProfileDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    completed: bool | None = None,
) -> MediaPage:
    items = context.history.history(profile_id, limit=limit, offset=offset,
                                    completed=completed)
    total = len(items) if offset == 0 and len(items) < limit else offset + len(items)
    return MediaPage(items=[MediaOut(**i) for i in items], total=total,
                     offset=offset, limit=limit, has_more=len(items) >= limit)


@router.get("/continue-watching", response_model=list[MediaOut],
            summary="Titles with progress but not finished")
async def continue_watching(
    context: Ctx, profile_id: ProfileDep,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> list[MediaOut]:
    return [MediaOut(**i) for i in context.history.continue_watching(profile_id, limit=limit)]


@router.get("/history/{media_id}", response_model=ProgressOut,
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


@router.put("/history/{media_id}", response_model=ProgressOut,
            summary="Record playback progress")
async def put_progress(media_id: MediaPathId, payload: ProgressUpdate, context: Ctx,
                       profile_id: ProfileDep) -> ProgressOut:
    result = context.history.record_progress(
        profile_id, media_id,
        position_seconds=payload.position_seconds,
        duration_seconds=payload.duration_seconds,
        completed=payload.completed,
    )
    return ProgressOut(**result.as_dict())


@router.delete("/history/{media_id}", response_model=OkResponse,
               summary="Remove one history entry")
async def delete_progress(media_id: MediaPathId, context: Ctx, profile_id: ProfileDep) -> OkResponse:
    removed = context.history.remove_entry(profile_id, media_id)
    if not removed:
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail="No history entry for this title.")
    return OkResponse(detail="History entry removed.")


@router.delete("/history", response_model=OkResponse,
               summary="Clear this profile's history")
async def clear_history(context: Ctx, profile_id: ProfileDep) -> OkResponse:
    removed = context.history.clear(profile_id)
    return OkResponse(detail=f"Removed {removed} history entr(y/ies).")


# ---------------------------------------------------------------------------
# Ratings
# ---------------------------------------------------------------------------
@router.put("/media/{media_id}/rating", response_model=RatingOut,
            summary="Rate a title for this profile")
async def rate(media_id: MediaPathId, payload: RatingUpdate, context: Ctx,
               profile_id: ProfileDep) -> RatingOut:
    row = context.ratings.rate(profile_id, media_id, payload.score)
    return RatingOut(media_id=media_id, score=float(row["score"]))


@router.delete("/media/{media_id}/rating", response_model=OkResponse,
               summary="Remove this profile's rating")
async def clear_rating(media_id: MediaPathId, context: Ctx, profile_id: ProfileDep) -> OkResponse:
    context.ratings.clear(profile_id, media_id)
    return OkResponse(detail="Rating removed.")


@router.get("/media/{media_id}/rating", response_model=RatingOut,
            summary="This profile's rating for a title")
async def get_rating(media_id: MediaPathId, context: Ctx, profile_id: ProfileDep) -> RatingOut:
    score = context.ratings.get(profile_id, media_id)
    return RatingOut(media_id=media_id, score=float(score or 0.0))


__all__ = ["router"]