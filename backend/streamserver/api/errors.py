"""Exception handlers.

The server's error contract, in one place:

* The body is always ``{"error": <machine code>, "message": <human text>,
  "request_id": ...}``.
* **No stack traces, SQL, file paths or library internals ever reach a
  client.**  An unexpected exception becomes a generic 500 with a request id
  that also appears in the server log, so an operator can correlate without the
  client learning anything.
* Known, expected failures (bad filter, wrong profile, missing file) keep their
  specific status and a message written for a human to read.
"""

from __future__ import annotations

import logging
import sqlite3
import traceback

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from ..db import repositories as repo
from ..services.auth import AuthError
from ..services.history import HistoryError
from ..services.importer import MediaImportError, PathNotImportable
from ..services.library import LibraryError
from ..services.profiles import (
    PinIncorrectError,
    PinRequiredError,
    ProfileAccessError,
    ProfileError,
)

log = logging.getLogger("streamserver.api")


def _body(error: str, message: str, request: Request, detail=None) -> dict:
    return {
        "error": error,
        "message": message,
        "detail": detail,
        "request_id": getattr(request.state, "request_id", None),
    }


def _response(status_code: int, error: str, message: str, request: Request,
              detail=None) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content=_body(error, message, request, detail),
    )


async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    detail = exc.detail
    if isinstance(detail, dict):
        message = str(detail.get("message") or detail.get("detail") or "Request failed.")
        error = str(detail.get("error") or "http_error")
    else:
        message = str(detail)
        error = _code_for_status(exc.status_code)
    headers = getattr(exc, "headers", None)
    return JSONResponse(
        status_code=exc.status_code,
        content=_body(error, message, request),
        headers=headers,
    )


async def starlette_http_handler(request: Request,
                                 exc: StarletteHTTPException) -> JSONResponse:
    return await http_exception_handler(request, HTTPException(
        status_code=exc.status_code, detail=exc.detail, headers=exc.headers
    ))


def _code_for_status(status_code: int) -> str:
    return {
        400: "bad_request",
        401: "unauthenticated",
        403: "forbidden",
        404: "not_found",
        405: "method_not_allowed",
        409: "conflict",
        413: "payload_too_large",
        415: "unsupported_media_type",
        422: "validation_error",
        423: "locked",
        429: "rate_limited",
        500: "internal_error",
        503: "unavailable",
    }.get(status_code, "http_error")


async def validation_error_handler(request: Request,
                                   exc: RequestValidationError) -> JSONResponse:
    """Turn Pydantic errors into the standard shape.

    Only the field location and the reason are returned.  ``exc.errors()`` can
    include the offending input, which for this API could be a password, so it
    is deliberately not echoed.
    """
    problems = []
    for error in exc.errors()[:20]:
        location = ".".join(str(part) for part in error.get("loc", ()) if part != "body")
        problems.append({"field": location or "body", "reason": error.get("msg", "invalid")})
    return _response(
        422, "validation_error",
        "The request body or parameters are not valid.", request, detail=problems,
    )


async def auth_error_handler(request: Request, exc: AuthError) -> JSONResponse:
    headers = {}
    if exc.retry_after:
        headers["Retry-After"] = str(exc.retry_after)
    return JSONResponse(
        status_code=exc.status_code,
        content=_body(exc.code, str(exc), request),
        headers=headers or None,
    )


async def profile_error_handler(request: Request, exc: ProfileError) -> JSONResponse:
    if isinstance(exc, PinIncorrectError) and exc.attempts_left is not None:
        return _response(
            exc.status_code, exc.code, str(exc), request,
            detail={"attempts_left": exc.attempts_left},
        )
    return _response(exc.status_code, exc.code, str(exc), request)


async def library_error_handler(request: Request, exc: LibraryError) -> JSONResponse:
    return _response(exc.status_code, exc.code, str(exc), request)


async def history_error_handler(request: Request, exc: HistoryError) -> JSONResponse:
    return _response(exc.status_code, exc.code, str(exc), request)


async def import_error_handler(request: Request, exc: MediaImportError) -> JSONResponse:
    # The message may quote a path.  The operator already knows their own
    # filesystem, and this endpoint is admin-only, so the detail is useful --
    # but the log and the response stay this precise only for PathNotImportable.
    return _response(400, "import_failed", str(exc), request)


async def sqlite_error_handler(request: Request, exc: sqlite3.Error) -> JSONResponse:
    # Never surface SQL text: it would describe the schema to an attacker.
    log.error("database error on %s %s: %s", request.method, request.url.path, exc)
    return _response(500, "database_error",
                     "A database error occurred. The request was not completed.", request)


async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    request_id = getattr(request.state, "request_id", None)
    log.error(
        "unhandled exception on %s %s (request_id=%s)\n%s",
        request.method, request.url.path, request_id,
        "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
    )
    return _response(
        500, "internal_error",
        "Something went wrong on the server. The incident was logged.",
        request,
    )


#: Exceptions that are re-raised deliberately and need no stack trace.
_KNOWN = (
    AuthError, ProfileError, LibraryError, HistoryError, MediaImportError,
    ProfileAccessError, PinRequiredError, PinIncorrectError,
)


def install_handlers(app: FastAPI) -> None:
    app.add_exception_handler(HTTPException, http_exception_handler)
    app.add_exception_handler(StarletteHTTPException, starlette_http_handler)
    app.add_exception_handler(RequestValidationError, validation_error_handler)
    app.add_exception_handler(AuthError, auth_error_handler)
    app.add_exception_handler(ProfileError, profile_error_handler)
    app.add_exception_handler(LibraryError, library_error_handler)
    app.add_exception_handler(HistoryError, history_error_handler)
    app.add_exception_handler(MediaImportError, import_error_handler)
    app.add_exception_handler(sqlite3.Error, sqlite_error_handler)
    app.add_exception_handler(Exception, unhandled_error_handler)


__all__ = ["install_handlers", "repo"]