"""Service layer: the business logic the API routes call.

Each service owns its own validation and raises a typed error that the API
layer maps onto an HTTP status.  Nothing here knows about HTTP.
"""

from .auth import AuthError, AuthService, TokenPair, validate_password_strength
from .history import HistoryService, RatingService
from .importer import ImportStats, Importer, MediaImportError, PathNotImportable
from .library import LibraryError, LibraryService
from .profiles import ProfileError, ProfileService, ProfileView

__all__ = [
    "AuthError", "AuthService", "TokenPair", "validate_password_strength",
    "HistoryService", "RatingService",
    "ImportStats", "Importer", "MediaImportError", "PathNotImportable",
    "LibraryError", "LibraryService",
    "ProfileError", "ProfileService", "ProfileView",
]
