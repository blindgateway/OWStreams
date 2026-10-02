"""Watch history, continue-watching state and per-profile ratings.

Progress semantics
------------------
A position becomes "started" once it passes ``start_threshold_seconds``, and
"completed" once it reaches ``completion_threshold`` of the runtime.  The
runtime used for the percentage is the *probed* duration when known, falling
back to the client's reported duration, so a progress bar never disagrees with
the metadata the user can see on the details screen.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from ..config import MediaConfig
from ..db import repositories as repo
from ..db.database import Database

log = logging.getLogger(__name__)

MIN_RATING = 0.5
MAX_RATING = 5.0


class HistoryError(Exception):
    def __init__(self, message: str, code: str = "invalid_progress",
                 status_code: int = 422) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


@dataclass(slots=True)
class ProgressResult:
    profile_id: int
    media_id: int
    position_seconds: float
    duration_seconds: float
    percent: float
    completed: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "profile_id": self.profile_id,
            "media_id": self.media_id,
            "position_seconds": self.position_seconds,
            "duration_seconds": self.duration_seconds,
            "percent": round(self.percent, 4),
            "completed": self.completed,
        }


class HistoryService:
    def __init__(self, db: Database, config: MediaConfig) -> None:
        self.db = db
        self.config = config

    # -- recording --------------------------------------------------------
    def record_progress(
        self,
        profile_id: int,
        media_id: int,
        *,
        position_seconds: float,
        duration_seconds: float | None = None,
        completed: bool | None = None,
    ) -> ProgressResult:
        media = repo.get_media(self.db, media_id)
        if not media:
            raise HistoryError("Unknown media item.", code="media_not_found", status_code=404)

        # Prefer the probed runtime: the player's own duration can be wrong
        # when it was produced by a broken container.
        runtime = media.get("runtime_minutes")
        reference = float(runtime) * 60.0 if runtime else 0.0
        client_duration = float(duration_seconds or 0.0)
        if client_duration > 0 and (reference <= 0 or abs(client_duration - reference) > 5.0):
            reference = client_duration if reference <= 0 else reference
        elif client_duration > 0:
            reference = reference

        try:
            position = float(position_seconds)
        except (TypeError, ValueError) as exc:
            raise HistoryError("position_seconds must be a number.") from exc

        if position < 0:
            raise HistoryError("position_seconds cannot be negative.")
        if reference > 0 and position > reference * 2:
            # A wildly out-of-range position is a client bug or an attempt to
            # poison the "continue watching" rail; clamp instead of storing it.
            log.info("clamping implausible progress for media %s: %s s", media_id, position)
            position = reference

        if completed is None:
            completed = reference > 0 and position >= reference * self.config.completion_threshold
        if reference <= 0:
            completed = False
            percent = 0.0
        else:
            percent = max(0.0, min(1.0, position / reference))
            if position <= self.config.start_threshold_seconds:
                percent = 0.0

        row = repo.upsert_progress(
            self.db, profile_id, media_id,
            position_seconds=position,
            duration_seconds=reference,
            completed=completed,
            percent=percent,
        )
        repo.increment_play_count(self.db, media_id)
        repo.clear_recommendations(self.db, profile_id)
        return ProgressResult(
            profile_id=profile_id,
            media_id=media_id,
            position_seconds=float(row["position_seconds"]),
            duration_seconds=float(row["duration_seconds"]),
            percent=float(row["percent"]),
            completed=bool(row["completed"]),
        )

    def mark_started(self, profile_id: int, media_id: int, start_offset: float = 0.0) -> ProgressResult:
        existing = repo.get_progress(self.db, profile_id, media_id)
        if existing and float(existing["position_seconds"]) > 0:
            # Returning to something already in progress keeps its position.
            return ProgressResult(
                profile_id=profile_id, media_id=media_id,
                position_seconds=float(existing["position_seconds"]),
                duration_seconds=float(existing["duration_seconds"]),
                percent=float(existing["percent"]),
                completed=bool(existing["completed"]),
            )
        return self.record_progress(profile_id, media_id, position_seconds=start_offset)

    # -- reading ----------------------------------------------------------
    def get_progress(self, profile_id: int, media_id: int) -> dict[str, Any] | None:
        return repo.get_progress(self.db, profile_id, media_id)

    def history(self, profile_id: int, *, limit: int = 50, offset: int = 0,
                completed: bool | None = None) -> list[dict[str, Any]]:
        return repo.history_entries(self.db, profile_id, limit=limit, offset=offset,
                                    completed=completed)

    def continue_watching(self, profile_id: int, *, limit: int = 20) -> list[dict[str, Any]]:
        return repo.continue_watching(
            self.db, profile_id, limit=limit,
            completion_threshold=self.config.completion_threshold,
        )

    def recently_watched(self, profile_id: int, *, limit: int = 20) -> list[dict[str, Any]]:
        return repo.history_entries(self.db, profile_id, limit=limit, offset=0)

    # -- clearing ---------------------------------------------------------
    def remove_entry(self, profile_id: int, media_id: int) -> bool:
        return repo.delete_progress(self.db, profile_id, media_id)

    def clear(self, profile_id: int, *, before: str | None = None) -> int:
        removed = repo.clear_history(self.db, profile_id, before=before)
        repo.clear_recommendations(self.db, profile_id)
        return removed

    def counts(self, profile_id: int) -> dict[str, int]:
        return {
            "total": repo.count_history(self.db, profile_id),
            "in_progress": len(repo.continue_watching(
                self.db, profile_id, limit=1000,
                completion_threshold=self.config.completion_threshold,
            )),
        }


class RatingService:
    """Per-profile ratings.  Deliberately separate from any external provider
    rating: the latter is reference data shown on the details page, this is the
    viewer's own opinion and never overwrites it."""

    def __init__(self, db: Database) -> None:
        self.db = db

    def rate(self, profile_id: int, media_id: int, score: float) -> dict[str, Any]:
        if not repo.get_media(self.db, media_id):
            raise HistoryError("Unknown media item.", code="media_not_found", status_code=404)
        try:
            value = float(score)
        except (TypeError, ValueError) as exc:
            raise HistoryError("score must be a number.") from exc
        if not (MIN_RATING <= value <= MAX_RATING):
            raise HistoryError(
                f"Rating must be between {MIN_RATING} and {MAX_RATING}."
            )
        # Snap to the nearest half star; half-star precision is the norm and
        # anything finer is noise nobody can express on a 10-point widget.
        value = round(value * 2) / 2
        return repo.set_rating(self.db, profile_id, media_id, value)

    def clear(self, profile_id: int, media_id: int) -> bool:
        return repo.delete_rating(self.db, profile_id, media_id)

    def get(self, profile_id: int, media_id: int) -> float | None:
        return repo.get_rating(self.db, profile_id, media_id)

    def map_for(self, profile_id: int, media_ids: list[int]) -> dict[int, float]:
        return repo.ratings_map(self.db, profile_id, media_ids)