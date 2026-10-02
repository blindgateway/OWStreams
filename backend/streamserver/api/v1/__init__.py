"""Version 1 of the API.

Versioning strategy
-------------------
The version lives in the path (``/api/v1``).  A breaking change gets a new
prefix; a backwards-compatible addition lands in the current prefix.  Within a
version, **no response field is removed or retyped**, no enum value disappears
and no required request field is added.  New optional request fields and new
response fields may be added at any time, which is why clients must ignore
unknown keys.

``/api/v1`` is stable for the 1.x series.  See API.md for the full contract.
"""

from fastapi import APIRouter

from . import admin_routes, auth_routes, library_routes, media_routes, profile_routes

api_router = APIRouter()
api_router.include_router(auth_routes.router)
api_router.include_router(profile_routes.router)
api_router.include_router(library_routes.router)
api_router.include_router(media_routes.router)
api_router.include_router(admin_routes.router)
# The streaming router is mounted alongside the others rather than inside the
# media router so its prefix is exactly /api/v1/stream -- the URL the client
# gets back from POST /media/{id}/playback.
api_router.include_router(media_routes.stream_router)

__all__ = ["api_router"]