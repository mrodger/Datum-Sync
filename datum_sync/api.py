"""REST API.

    uvicorn datum_sync.api:app --host 0.0.0.0 --port 8200

Programmatic endpoints live under `/rest/v1/`. The three service paths
(`/stream`, `/download`, `/upload`) sit at the root because they are the URLs
handed to end users and embedded in other people's applications.

Every route requires a bearer token except the handful in `PUBLIC_PATHS`, and
every route naming a repository checks the caller is scoped to it. The check is
a middleware rather than a per-route dependency; the reasoning is at
`PUBLIC_PATHS` below.

The synchronous services do not run anything themselves: they submit to the
same queue the async endpoint uses and wait for the result. Executing a
workspace inside the request handler would put every guarantee the worker
provides -- concurrency limit, timeout, cancellation, one child per job --
behind an unbounded number of concurrent HTTP connections.
"""
from __future__ import annotations

import io
import json
import logging
import uuid
import zipfile
from contextlib import asynccontextmanager
from typing import Any

import asyncpg
from fastapi import Body, Depends, FastAPI, Header, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
# Not fastapi.UploadFile: that is a *subclass*, used for signature annotations.
# `request.form()` yields the starlette base class, so an isinstance check
# against the FastAPI one silently matches nothing and every file is treated as
# an ordinary text field.
from starlette.datastructures import UploadFile

from datum_sync import auth, config, db, errors, events, execute, jobs, mcp, oauth, uploads
from datum_sync.auth import Principal
from datum_sync.errors import ApiError

log = logging.getLogger("datum_sync")

# uvicorn configures its own loggers and leaves the root alone, so a record
# logged here propagates to a root with no handler and is discarded. Found by
# launching the server and seeing the startup line simply absent -- no test
# catches this, because nothing under ASGITransport runs the lifespan. A
# diagnostic that silently prints nothing is worse than none at all, so attach a
# handler when, and only when, nothing upstream would have printed it.
if not log.handlers and not logging.getLogger().handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(levelname)s:     %(message)s"))
    log.addHandler(_handler)
    log.setLevel(logging.INFO)

MAX_UPLOAD_BYTES = 200 * 1024 * 1024

# Everything not named here requires a bearer token. An allowlist rather than a
# per-route dependency because the failure modes are not symmetric: forgetting
# to add a route here makes it return 401 and someone complains, while
# forgetting a `Depends(...)` publishes it and nobody notices.
PUBLIC_PATHS = frozenset(
    {
        "/health",
        # OAuth's own endpoints cannot require the credential they exist to
        # issue. Each authenticates by its own rules -- PKCE, the consent
        # screen's password, the client secret.
        "/.well-known/oauth-authorization-server",
        "/.well-known/oauth-protected-resource",
        "/oauth/register",
        "/oauth/authorize",
        "/oauth/token",
        "/oauth/revoke",
        # The interface description, not the data behind it. Every route it
        # lists still refuses an unauthenticated call.
        "/docs",
        "/openapi.json",
    }
)


@asynccontextmanager
async def lifespan(_: FastAPI):
    # Before the pool, so a misconfigured server fails on the setting rather
    # than on whatever the setting later breaks. Logged even when valid: a
    # PUBLIC_URL that is set but wrong fails exactly like one that is right,
    # until a client tries to use the token, so the resolved value needs to be
    # visible at startup rather than inferred from a failure.
    log.info("public URL: %s", config.require_public_url())
    await db.init_pool()
    try:
        yield
    finally:
        await db.close_pool()


app = FastAPI(
    title="Datum-Sync",
    version="0.1.0",
    lifespan=lifespan,
    docs_url="/docs",
    openapi_url="/openapi.json",
)
errors.install(app)
oauth.install(app)
app.include_router(mcp.router)


@app.middleware("http")
async def authenticate(request: Request, call_next):
    """Fail closed: no bearer token, no route.

    The envelope is built here rather than raised, because middleware sits
    *outside* Starlette's exception middleware -- an ApiError raised at this
    point would surface as a bare 500 with the WWW-Authenticate challenge
    stripped, which is precisely the header an MCP client needs.
    """
    if request.url.path in PUBLIC_PATHS:
        return await call_next(request)
    try:
        request.state.principal = await auth.require_auth(request)
    except ApiError as exc:
        return errors.envelope(
            exc.status, exc.code, exc.message, exc.detail, exc.headers
        )
    return await call_next(request)


# -- helpers ---------------------------------------------------------------


def _caller(request: Request) -> Principal:
    return request.state.principal


Caller = Depends(_caller)


def _job_id(raw: str) -> uuid.UUID:
    try:
        return uuid.UUID(raw)
    except (ValueError, AttributeError):
        raise ApiError(400, "INVALID_PARAMETER", f"not a job id: {raw!r}") from None


async def _job_for(
    conn: asyncpg.Connection, caller: Principal, job_id: uuid.UUID
) -> asyncpg.Record:
    """Fetch a job, refusing one in a repository the caller is not scoped to.

    The job's repository is the denormalised text on the row, not a lookup:
    scope has to be checked against what the job actually ran, which survives
    the workspace being unpublished.
    """
    row = await execute.job_row(conn, job_id)
    auth.require_repo(caller, row["repository"])
    return row


def _job_json(row: asyncpg.Record) -> dict[str, Any]:
    def when(key: str) -> str | None:
        value = row[key]
        return value.isoformat() if value is not None else None

    return {
        "id": str(row["id"]),
        "repository": row["repository"],
        "workspace": row["workspace"],
        "status": row["status"],
        "params": json.loads(row["params"]),
        "artifacts": json.loads(row["artifacts"]),
        "error": row["error"],
        "submitted_by": row["submitted_by"],
        "parent_job": str(row["parent_job"]) if row["parent_job"] else None,
        "submitted_at": when("submitted_at"),
        "started_at": when("started_at"),
        "completed_at": when("completed_at"),
    }



# -- system ----------------------------------------------------------------


@app.get("/health")
async def health() -> dict[str, Any]:
    try:
        async with db.pool().acquire() as conn:
            await conn.fetchval("SELECT 1")
            worker = await execute.worker_is_running(conn)
    except Exception as e:
        raise ApiError(
            503, "SERVICE_UNAVAILABLE", f"database unreachable: {e}"
        ) from None
    return {"status": "ok", "database": "ok", "worker": "running" if worker else "down"}


@app.get("/rest/v1/engines")
async def engines() -> dict[str, Any]:
    async with db.pool().acquire() as conn:
        running = await execute.worker_is_running(conn)
        queued = await conn.fetchval(
            "SELECT count(*) FROM jobs WHERE status = 'queued'"
        )
        active = await conn.fetchval(
            "SELECT count(*) FROM jobs WHERE status = 'running'"
        )
    return {
        "workers": 1 if running else 0,
        "queued": queued,
        "running": active,
    }


# -- repositories and workspaces -------------------------------------------


@app.get("/rest/v1/repositories")
async def list_repositories(caller: Principal = Caller) -> dict[str, Any]:
    async with db.pool().acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT r.name, r.path, count(w.id) AS workspaces
            FROM repositories r LEFT JOIN workspaces w ON w.repository_id = r.id
            GROUP BY r.id ORDER BY r.name
            """
        )
    # Filtered, not refused: a listing is the one place where out-of-scope
    # repositories should simply not appear.
    return {"items": [dict(r) for r in rows if caller.allows_repo(r["name"])]}


@app.get("/rest/v1/repositories/{repo}")
async def get_repository(repo: str, caller: Principal = Caller) -> dict[str, Any]:
    auth.require_repo(caller, repo)
    async with db.pool().acquire() as conn:
        row = await conn.fetchrow(
            "SELECT name, path FROM repositories WHERE name = $1", repo
        )
        if row is None:
            raise ApiError(404, "NOT_FOUND", f"no such repository {repo}")
        names = await conn.fetch(
            """
            SELECT w.name FROM workspaces w JOIN repositories r
              ON r.id = w.repository_id
            WHERE r.name = $1 ORDER BY w.name
            """,
            repo,
        )
    return {**dict(row), "workspaces": [r["name"] for r in names]}


@app.get("/rest/v1/repositories/{repo}/workspaces")
async def list_workspaces(repo: str, caller: Principal = Caller) -> dict[str, Any]:
    auth.require_repo(caller, repo)
    async with db.pool().acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT w.name, w.description, w.version, w.published_at
            FROM workspaces w JOIN repositories r ON r.id = w.repository_id
            WHERE r.name = $1 ORDER BY w.name
            """,
            repo,
        )
    return {
        "items": [
            {**dict(r), "published_at": r["published_at"].isoformat()} for r in rows
        ]
    }


@app.get("/rest/v1/repositories/{repo}/workspaces/{ws}")
async def get_workspace(
    repo: str, ws: str, caller: Principal = Caller
) -> dict[str, Any]:
    auth.require_repo(caller, repo)
    async with db.pool().acquire() as conn:
        manifest = await jobs.load_manifest_for(conn, repo, ws)
    return {"repository": repo, **manifest.model_dump(mode="json")}


# -- job submission --------------------------------------------------------


@app.post("/rest/v1/transformations/submit/{repo}/{ws}", status_code=202)
async def submit(
    repo: str,
    ws: str,
    response: Response,
    body: dict[str, Any] = Body(default_factory=dict),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    caller: Principal = Caller,
) -> dict[str, Any]:
    auth.require_repo(caller, repo)
    params = body.get("params", {})
    if not isinstance(params, dict):
        raise ApiError(400, "INVALID_PARAMETER", "'params' must be an object")

    async with db.pool().acquire() as conn:
        await execute.manifest_for(conn, repo, ws, "job_submitter")
        job_id = await jobs.submit_idempotent(
            conn,
            repo,
            ws,
            params,
            submitted_by=caller.name,
            idempotency_key=idempotency_key,
        )
    response.headers["Location"] = f"/rest/v1/transformations/jobs/id/{job_id}"
    return {"id": str(job_id), "status": "queued"}


@app.get("/rest/v1/transformations/jobs/id/{raw_id}")
async def get_job(raw_id: str, caller: Principal = Caller) -> dict[str, Any]:
    async with db.pool().acquire() as conn:
        return _job_json(await _job_for(conn, caller, _job_id(raw_id)))


@app.delete("/rest/v1/transformations/jobs/id/{raw_id}")
async def cancel_job(raw_id: str, caller: Principal = Caller) -> dict[str, Any]:
    job_id = _job_id(raw_id)
    async with db.pool().acquire() as conn:
        await _job_for(conn, caller, job_id)
        result = await jobs.cancel(conn, job_id)
    # 'signalled' means a worker was asked to stop a running child: the job is
    # not cancelled yet, and saying so would be a lie the caller acts on.
    return {"id": raw_id, "result": result}


@app.post("/rest/v1/transformations/jobs/id/{raw_id}/resubmit", status_code=202)
async def resubmit_job(
    raw_id: str, response: Response, caller: Principal = Caller
) -> dict[str, Any]:
    job_id = _job_id(raw_id)
    async with db.pool().acquire() as conn:
        row = await _job_for(conn, caller, job_id)
        new_id = await jobs.submit(
            conn,
            row["repository"],
            row["workspace"],
            json.loads(row["params"]),
            # Whoever re-ran it, not whoever ran it first: the audit trail
            # should name the account that caused this execution.
            submitted_by=caller.name,
            parent_job=job_id,
        )
    response.headers["Location"] = f"/rest/v1/transformations/jobs/id/{new_id}"
    return {"id": str(new_id), "status": "queued", "parent_job": raw_id}


@app.get("/rest/v1/transformations/jobs/id/{raw_id}/log")
async def get_job_log(
    raw_id: str,
    after_id: int = Query(default=0, ge=0),
    caller: Principal = Caller,
) -> dict[str, Any]:
    job_id = _job_id(raw_id)
    async with db.pool().acquire() as conn:
        await _job_for(conn, caller, job_id)
        entries = await jobs.get_log(conn, job_id, after_id=after_id)
    return {
        "items": [
            {"id": e["id"], "ts": e["ts"].isoformat(), "level": e["level"],
             "message": e["message"]}
            for e in entries
        ]
    }


@app.get("/rest/v1/transformations/jobs/id/{raw_id}/events")
async def stream_job_events(
    raw_id: str,
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    caller: Principal = Caller,
) -> StreamingResponse:
    """SSE feed for one job: status, log and progress until it finishes."""
    job_id = _job_id(raw_id)
    async with db.pool().acquire() as conn:
        await _job_for(conn, caller, job_id)
    try:
        after_id = int(last_event_id) if last_event_id else 0
    except ValueError:
        after_id = 0

    async def body():
        # Its own connection: LISTEN is per-session and this one is held for
        # as long as the client stays subscribed.
        conn = await asyncpg.connect(config.DATABASE_URL)
        try:
            async for frame in events.job_events(conn, job_id, after_id=after_id):
                yield frame
        finally:
            await conn.close()

    return StreamingResponse(
        body(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/rest/v1/transformations/jobs/id/{raw_id}/artifacts/{name}")
async def get_artifact(
    raw_id: str, name: str, caller: Principal = Caller
) -> FileResponse:
    job_id = _job_id(raw_id)
    async with db.pool().acquire() as conn:
        row = await _job_for(conn, caller, job_id)

    artifact = next(
        (a for a in json.loads(row["artifacts"]) if a["name"] == name), None
    )
    if artifact is None:
        raise ApiError(404, "NOT_FOUND", f"job {job_id} has no artifact {name!r}")
    return FileResponse(
        execute.artifact_path(job_id, artifact["file"]),
        media_type=artifact["type"],
        filename=artifact["file"],
    )


# -- service paths ---------------------------------------------------------


@app.get("/stream/{repo}/{ws}")
async def data_streaming(
    repo: str, ws: str, request: Request, caller: Principal = Caller
) -> Response:
    """Run and return the primary output inline. Parameters come from the query."""
    auth.require_repo(caller, repo)
    row, manifest = await execute.run_sync(
        repo, ws, dict(request.query_params), "data_streaming", caller.name
    )
    primary = manifest.primary_output
    artifacts = json.loads(row["artifacts"])
    chosen = next(
        (a for a in artifacts if primary and a["name"] == primary.name), None
    )
    if chosen is None:
        raise ApiError(
            502,
            "NO_PRIMARY_OUTPUT",
            "the run produced no primary output to stream",
            {"job_id": str(row["id"]),
             "produced": [a["name"] for a in artifacts]},
        )
    path = execute.artifact_path(row["id"], chosen["file"])
    return Response(content=path.read_bytes(), media_type=chosen["type"])


@app.get("/download/{repo}/{ws}")
async def data_download(
    repo: str, ws: str, request: Request, caller: Principal = Caller
) -> Response:
    """Run and return the outputs as an attachment: the file if there is one,
    a zip if there are several."""
    auth.require_repo(caller, repo)
    row, _ = await execute.run_sync(
        repo, ws, dict(request.query_params), "data_download", caller.name
    )
    artifacts = json.loads(row["artifacts"])
    if not artifacts:
        raise ApiError(
            502, "NO_OUTPUT", "the run produced no outputs to download",
            {"job_id": str(row["id"])},
        )

    if len(artifacts) == 1:
        only = artifacts[0]
        return FileResponse(
            execute.artifact_path(row["id"], only["file"]),
            media_type=only["type"],
            filename=only["file"],
        )

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for a in artifacts:
            archive.write(execute.artifact_path(row["id"], a["file"]), arcname=a["file"])
    return Response(
        content=buffer.getvalue(),
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{ws}-{row["id"]}.zip"'
        },
    )


@app.post("/upload/{repo}/{ws}", status_code=202)
async def data_upload(
    repo: str,
    ws: str,
    request: Request,
    response: Response,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    caller: Principal = Caller,
) -> dict[str, Any]:
    """Take multipart files and submit a job.

    Each file field becomes the parameter of the same name, carrying the
    upload id. Non-file fields are passed through as ordinary parameters.
    """
    auth.require_repo(caller, repo)
    async with db.pool().acquire() as conn:
        # Before the body is read, not after: writing the files first would let
        # an unknown workspace name orphan 200MB on disk for every request that
        # then 404s.
        await execute.manifest_for(conn, repo, ws, "data_upload")

    form = await request.form()
    params: dict[str, Any] = {}
    received: dict[str, str] = {}

    try:
        for field, value in form.multi_items():
            if isinstance(value, UploadFile):
                data = await value.read()
                if len(data) > MAX_UPLOAD_BYTES:
                    raise ApiError(
                        413, "PAYLOAD_TOO_LARGE",
                        f"{field}: {len(data)} bytes exceeds the "
                        f"{MAX_UPLOAD_BYTES} byte limit",
                    )
                upload_id = uploads.save(value.filename, data)
                params[field] = upload_id
                received[field] = uploads.safe_name(value.filename)
            else:
                params[field] = value
    finally:
        await form.close()

    async with db.pool().acquire() as conn:
        job_id = await jobs.submit_idempotent(
            conn,
            repo,
            ws,
            params,
            submitted_by=caller.name,
            idempotency_key=idempotency_key,
        )
    response.headers["Location"] = f"/rest/v1/transformations/jobs/id/{job_id}"
    return {"id": str(job_id), "status": "queued", "files": received}


def main() -> int:
    import uvicorn

    uvicorn.run(app, host=config.HOST, port=config.PORT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
