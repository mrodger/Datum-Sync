"""One error shape for the whole API.

    {"status": 400, "code": "INVALID_PARAMETER", "message": "...",
     "detail": {}, "trace_id": "..."}

A client that has to parse three different error shapes -- FastAPI's
`{"detail": ...}`, a raised exception's string, and whatever a handler
returned -- ends up matching on message text. Every failure leaves here in the
same envelope so `code` is the thing to branch on.

`trace_id` is the server-minted id from `audit.Trace`, and it is the whole
reason the trace is minted in a middleware rather than in each route: it joins
this response to the `audit_log` row for the same request. Without it a caller
reporting a failure can offer a timestamp and a path, and someone has to guess
which row that was.

It is a required parameter of `envelope()` rather than an optional one, and
`envelope()` takes the whole `Request` rather than a trace string. Both are
deliberate. The alternative -- a module-level ContextVar that `envelope()` reads
with no signature change -- costs nothing at the call sites and would have been
less code, but its failure mode is that the var is never set and every error
carries `"trace_id": null` while every test that checks the envelope's *shape*
still passes. A required parameter cannot be forgotten by a handler added later,
because the call does not work without it.
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
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.detail = detail or {}
        # Some failures are only actionable through a header: a 401 carries the
        # RFC 9728 `WWW-Authenticate` challenge that tells an MCP client where
        # to discover the authorization server. The body is no substitute --
        # the client reads the header.
        self.headers = headers or {}


def envelope(
    request: Request,
    status: int,
    code: str,
    message: str,
    detail: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    """Build the one error shape, carrying this request's trace id.

    `trace` is read with a default rather than as `request.state.trace`, so that
    a missing trace costs a null field instead of turning every handled 4xx into
    an unhandled 500 -- an error path is the worst place to add a second way to
    fail. That leniency is what `tests/test_error_trace.py` exists to make safe:
    it asserts the id in the body is the same id as in this request's `audit_log`
    row, so the trace going missing fails a test rather than going quiet.
    """
    trace = getattr(request.state, "trace", None)
    return JSONResponse(
        status_code=status,
        content={
            "status": status,
            "code": code,
            "message": message,
            "detail": detail or {},
            "trace_id": trace.id if trace else None,
        },
        headers=headers,
    )


def install(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api_error(request: Request, exc: ApiError) -> JSONResponse:
        return envelope(
            request, exc.status, exc.code, exc.message, exc.detail, exc.headers
        )

    @app.exception_handler(WorkspaceNotFound)
    async def _not_found(request: Request, exc: WorkspaceNotFound) -> JSONResponse:
        return envelope(request, 404, "NOT_FOUND", str(exc))

    @app.exception_handler(JobError)
    async def _job_error(request: Request, exc: JobError) -> JSONResponse:
        # Parameter validation lives in jobs.submit, so this is the usual way a
        # bad request surfaces.
        return envelope(request, 400, "INVALID_PARAMETER", str(exc))

    @app.exception_handler(UploadError)
    async def _upload_error(request: Request, exc: UploadError) -> JSONResponse:
        return envelope(request, 400, "INVALID_PARAMETER", str(exc))

    @app.exception_handler(ManifestError)
    async def _manifest_error(request: Request, exc: ManifestError) -> JSONResponse:
        # A published manifest failed to parse: the database holds something
        # this build cannot read, which is a server fault, not the caller's.
        return envelope(request, 500, "INVALID_MANIFEST", str(exc))

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = _BY_STATUS.get(exc.status_code, "ERROR")
        return envelope(request, exc.status_code, code, str(exc.detail))

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        return envelope(
            request,
            422,
            "INVALID_PARAMETER",
            "request body or query is not valid",
            {"errors": exc.errors()},
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        """The catch-all: an exception nothing else claimed.

        Without this, Starlette's ServerErrorMiddleware answers a crash with a
        plain-text "Internal Server Error" -- no envelope, no `code`, and no
        trace id. That is the response a caller is most likely to be reporting,
        and it was the one response carrying nothing to look up. Measured, not
        assumed: `curl` on an unhandled exception returned 21 bytes of
        `text/plain`.

        Registering `Exception` is what installs a handler on
        ServerErrorMiddleware, which sits *outside* every user middleware -- so
        this is the only place a crash can be given the envelope shape.
        Starlette re-raises after calling this, so the traceback still reaches
        the server log; this changes the response, not the reporting.

        `str(exc)` is deliberately not in the message. An unhandled exception's
        text is written for a developer reading a traceback, and can carry a
        path, a query, or a connection string. The trace id is what makes this
        actionable, and it points at the log entry that does have the detail.
        """
        return envelope(
            request, 500, "INTERNAL_ERROR", "the server failed to handle this request"
        )
