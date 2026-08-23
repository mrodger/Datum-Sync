"""One error shape for the whole API.

    {"status": 400, "code": "INVALID_PARAMETER", "message": "...", "detail": {}}

A client that has to parse three different error shapes -- FastAPI's
`{"detail": ...}`, a raised exception's string, and whatever a handler
returned -- ends up matching on message text. Every failure leaves here in the
same envelope so `code` is the thing to branch on.
"""
from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from datum_sync.jobs import JobError, WorkspaceNotFound
from datum_sync.manifest import ManifestError
from datum_sync.uploads import UploadError

# Default codes by status, for errors raised as a bare HTTPException.
_BY_STATUS = {
    400: "BAD_REQUEST",
    401: "UNAUTHENTICATED",
    403: "FORBIDDEN",
    404: "NOT_FOUND",
    405: "METHOD_NOT_ALLOWED",
    409: "CONFLICT",
    413: "PAYLOAD_TOO_LARGE",
    422: "INVALID_PARAMETER",
    500: "INTERNAL_ERROR",
    503: "SERVICE_UNAVAILABLE",
    504: "TIMEOUT",
}


class ApiError(Exception):
    """An error with a status and a machine-readable code."""

    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        detail: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.detail = detail or {}


def envelope(
    status: int, code: str, message: str, detail: dict[str, Any] | None = None
) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={
            "status": status,
            "code": code,
            "message": message,
            "detail": detail or {},
        },
    )


def install(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api_error(_: Request, exc: ApiError) -> JSONResponse:
        return envelope(exc.status, exc.code, exc.message, exc.detail)

    @app.exception_handler(WorkspaceNotFound)
    async def _not_found(_: Request, exc: WorkspaceNotFound) -> JSONResponse:
        return envelope(404, "NOT_FOUND", str(exc))

    @app.exception_handler(JobError)
    async def _job_error(_: Request, exc: JobError) -> JSONResponse:
        # Parameter validation lives in jobs.submit, so this is the usual way a
        # bad request surfaces.
        return envelope(400, "INVALID_PARAMETER", str(exc))

    @app.exception_handler(UploadError)
    async def _upload_error(_: Request, exc: UploadError) -> JSONResponse:
        return envelope(400, "INVALID_PARAMETER", str(exc))

    @app.exception_handler(ManifestError)
    async def _manifest_error(_: Request, exc: ManifestError) -> JSONResponse:
        # A published manifest failed to parse: the database holds something
        # this build cannot read, which is a server fault, not the caller's.
        return envelope(500, "INVALID_MANIFEST", str(exc))

    @app.exception_handler(StarletteHTTPException)
    async def _http(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = _BY_STATUS.get(exc.status_code, "ERROR")
        return envelope(exc.status_code, code, str(exc.detail))

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError) -> JSONResponse:
        return envelope(
            422,
            "INVALID_PARAMETER",
            "request body or query is not valid",
            {"errors": exc.errors()},
        )
