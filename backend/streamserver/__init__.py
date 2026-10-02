"""Stream server: media library, playback and per-profile state.

Public entry point is :func:`streamserver.app.create_app`, which builds a
configured :class:`fastapi.FastAPI` instance bound to a :class:`~streamserver.db.database.Database`
and a :class:`~streamserver.config.Config`.
"""

__version__ = "1.0.0"
API_VERSION = "v1"

__all__ = ["__version__", "API_VERSION"]
