"""REST API.

    uvicorn datum_sync.api:app --host 0.0.0.0 --port 8200

Programmatic endpoints live under `/rest/v1/`. The three service paths
(`/stream`, `/download`, `/upload`) sit at the root because they are the URLs
handed to end users and embedded in other people's applications.

No authentication yet -- that is the next build step. Until then this must not
be exposed beyond localhost.

The synchronous services do not run anything themselves: they submit to the
same queue the async endpoint uses and wait for the result. Executing a
workspace inside the request handler would put every guarantee the worker
provides -- concurrency limit, timeout, cancellation, one child per job --
behind an unbounded number of concurrent HTTP connections.
"""
from __future__ import annotations

import asyncio
import io
import json
import uuid
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import asyncpg
from fastapi import Body, FastAPI, Header, Query, Request
from fastapi.responses import FileResponse, Response, StreamingResponse
# Not fastapi.UploadFile: that is a *subclass*, used for signature annotations.
# `request.form()` yields the starlette base class, so an isinstance check
# against the FastAPI one silently matches nothing and every file is treated as
# an ordinary text field.
from starlette.datastructures import UploadFile

from datum_sync import config, db, errors, events, jobs, uploads
from datum_sync.errors import ApiError
from datum_sync.manifest import Manifest
from datum_sync.worker import WORKER_LOCK

# Beyond the workspace's own timeout the job is being killed anyway; the extra
# margin covers the claim delay and the finish write.
SYNC_MARGIN_SECONDS = 15.0

MAX_UPLOAD_BYTES = 200 * 1024 * 1024


@asynccontextmanager
async def lifespan(_: FastAPI):
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


# -- helpers ---------------------------------------------------------------


def _job_id(raw: str) -> uuid.UUID:
    try:
        return uuid.UUID(raw)
    except (ValueError, AttributeError):
        raise ApiError(400, "INVALID_PARAMETER", f"not a job id: {raw!r}") from None


async def _job_row(conn: asyncpg.Connection, job_id: uuid.UUID) -> asyncpg.Record:
    row = await jobs.get(conn, job_id)
    if row is None:
        raise ApiError(404, "NOT_FOUND", f"no such job {job_id}")
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


async def _manifest(
    conn: asyncpg.Connection, repo: str, ws: str, service: str | None = None
) -> Manifest:
    manifest = await jobs.load_manifest_for(conn, repo, ws)
    if service is not None and service not in manifest.services:
        # Silently running it anyway would make `services` decorative, and a
        # workspace author's decision not to expose an endpoint meaningless.
        raise ApiError(
            404,
            "SERVICE_NOT_ENABLED",
            f"{repo}/{ws} does not publish the {service} service",
            {"published": manifest.services},
        )
    return manifest


async def _worker_is_running(conn: asyncpg.Connection) -> bool:
    """True if some process holds the worker advisory lock.

    Read from pg_locks rather than by trying to take the lock: acquiring it,
    even for an instant, would make a worker starting in that window exit with
    "another worker holds the lock".
    """
    return bool(
        await conn.fetchval(
            """
            SELECT 1 FROM pg_locks
            WHERE locktype = 'advisory' AND granted
              AND classid = $1 AND objid = $2
            LIMIT 1
            """,
            WORKER_LOCK >> 32,
            WORKER_LOCK & 0xFFFF_FFFF,
        )
    )


async def _await_job(job_id: uuid.UUID, timeout: float) -> asyncpg.Record:
    """Block until a job reaches a terminal status.

    Its own connection, held for the duration: LISTEN is per-session, so this
    cannot share the pool connection the caller used.
    """
    conn = await asyncpg.connect(config.DATABASE_URL)
    done = asyncio.Event()
    wanted = str(job_id)

    def on_notify(_c, _pid, _ch, payload: str) -> None:
        try:
            body = json.loads(payload)
        except json.JSONDecodeError:
            return
        if body.get("job_id") == wanted and body.get("status") in jobs.TERMINAL:
            done.set()

    try:
        await conn.add_listener(jobs.CHANNEL, on_notify)
        row = await _job_row(conn, job_id)
        if row["status"] in jobs.TERMINAL:
            return row
        try:
            await asyncio.wait_for(done.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            raise ApiError(
                504,
                "TIMEOUT",
                f"job did not finish within {timeout:.0f}s; it is still running",
                {"job_id": wanted},
            ) from None
        return await _job_row(conn, job_id)
    finally:
        await conn.close()


def _artifact_path(job_id: uuid.UUID, filename: str) -> Path:
    """Resolve a stored artifact, refusing anything outside the job's dir."""
    root = (config.DATA_PATH / "jobs" / str(job_id)).resolve()
    path = (root / filename).resolve()
    if not path.is_file() or root not in path.parents:
        raise ApiError(404, "NOT_FOUND", f"artifact file missing: {filename}")
    return path


async def _run_sync(
    repo: str, ws: str, params: dict[str, Any], service: str
) -> tuple[asyncpg.Record, Manifest]:
    """Submit and wait. Shared by /stream and /download."""
    async with db.pool().acquire() as conn:
        manifest = await _manifest(conn, repo, ws, service)
        if not await _worker_is_running(conn):
            raise ApiError(
                503,
                "NO_WORKER",
                "no worker is running; the job would queue indefinitely",
            )
        job_id = await jobs.submit(conn, repo, ws, params)

    row = await _await_job(job_id, manifest.timeout_seconds + SYNC_MARGIN_SECONDS)
    if row["status"] != "complete":
        raise ApiError(
            502,
            "JOB_FAILED",
            row["error"] or f"job {row['status']}",
            {"job_id": str(job_id), "status": row["status"]},
        )
    return row, manifest


# -- system ----------------------------------------------------------------


@app.get("/health")
async def health() -> dict[str, Any]:
    try:
        async with db.pool().acquire() as conn:
            await conn.fetchval("SELECT 1")
            worker = await _worker_is_running(conn)
    except Exception as e:
        raise ApiError(
            503, "SERVICE_UNAVAILABLE", f"database unreachable: {e}"
        ) from None
    return {"status": "ok", "database": "ok", "worker": "running" if worker else "down"}


@app.get("/rest/v1/engines")
async def engines() -> dict[str, Any]:
    async with db.pool().acquire() as conn:
        running = await _worker_is_running(conn)
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
async def list_repositories() -> dict[str, Any]:
    async with db.pool().acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT r.name, r.path, count(w.id) AS workspaces
            FROM repositories r LEFT JOIN workspaces w ON w.repository_id = r.id
            GROUP BY r.id ORDER BY r.name
            """
        )
    return {"items": [dict(r) for r in rows]}


@app.get("/rest/v1/repositories/{repo}")
async def get_repository(repo: str) -> dict[str, Any]:
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
async def list_workspaces(repo: str) -> dict[str, Any]:
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
async def get_workspace(repo: str, ws: str) -> dict[str, Any]:
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
) -> dict[str, Any]:
    params = body.get("params", {})
    if not isinstance(params, dict):
        raise ApiError(400, "INVALID_PARAMETER", "'params' must be an object")

    async with db.pool().acquire() as conn:
        await _manifest(conn, repo, ws, "job_submitter")
        job_id = await jobs.submit_idempotent(
            conn, repo, ws, params, idempotency_key=idempotency_key
        )
    response.headers["Location"] = f"/rest/v1/transformations/jobs/id/{job_id}"
    return {"id": str(job_id), "status": "queued"}


@app.get("/rest/v1/transformations/jobs/id/{raw_id}")
async def get_job(raw_id: str) -> dict[str, Any]:
    async with db.pool().acquire() as conn:
        return _job_json(await _job_row(conn, _job_id(raw_id)))


@app.delete("/rest/v1/transformations/jobs/id/{raw_id}")
async def cancel_job(raw_id: str) -> dict[str, Any]:
    job_id = _job_id(raw_id)
    async with db.pool().acquire() as conn:
        result = await jobs.cancel(conn, job_id)
    # 'signalled' means a worker was asked to stop a running child: the job is
    # not cancelled yet, and saying so would be a lie the caller acts on.
    return {"id": raw_id, "result": result}


@app.post("/rest/v1/transformations/jobs/id/{raw_id}/resubmit", status_code=202)
async def resubmit_job(raw_id: str, response: Response) -> dict[str, Any]:
    job_id = _job_id(raw_id)
    async with db.pool().acquire() as conn:
        row = await _job_row(conn, job_id)
        new_id = await jobs.submit(
            conn,
            row["repository"],
            row["workspace"],
            json.loads(row["params"]),
            submitted_by=row["submitted_by"],
            parent_job=job_id,
        )
    response.headers["Location"] = f"/rest/v1/transformations/jobs/id/{new_id}"
    return {"id": str(new_id), "status": "queued", "parent_job": raw_id}


@app.get("/rest/v1/transformations/jobs/id/{raw_id}/log")
async def get_job_log(raw_id: str, after_id: int = Query(default=0, ge=0)) -> dict[str, Any]:
    job_id = _job_id(raw_id)
    async with db.pool().acquire() as conn:
        await _job_row(conn, job_id)
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
) -> StreamingResponse:
    """SSE feed for one job: status, log and progress until it finishes."""
    job_id = _job_id(raw_id)
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
async def get_artifact(raw_id: str, name: str) -> FileResponse:
    job_id = _job_id(raw_id)
    async with db.pool().acquire() as conn:
        row = await _job_row(conn, job_id)

    artifact = next(
        (a for a in json.loads(row["artifacts"]) if a["name"] == name), None
    )
    if artifact is None:
        raise ApiError(404, "NOT_FOUND", f"job {job_id} has no artifact {name!r}")
    return FileResponse(
        _artifact_path(job_id, artifact["file"]),
        media_type=artifact["type"],
        filename=artifact["file"],
    )


# -- service paths ---------------------------------------------------------


@app.get("/stream/{repo}/{ws}")
async def data_streaming(repo: str, ws: str, request: Request) -> Response:
    """Run and return the primary output inline. Parameters come from the query."""
    row, manifest = await _run_sync(
        repo, ws, dict(request.query_params), "data_streaming"
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
    path = _artifact_path(row["id"], chosen["file"])
    return Response(content=path.read_bytes(), media_type=chosen["type"])


@app.get("/download/{repo}/{ws}")
async def data_download(repo: str, ws: str, request: Request) -> Response:
    """Run and return the outputs as an attachment: the file if there is one,
    a zip if there are several."""
    row, _ = await _run_sync(repo, ws, dict(request.query_params), "data_download")
    artifacts = json.loads(row["artifacts"])
    if not artifacts:
        raise ApiError(
            502, "NO_OUTPUT", "the run produced no outputs to download",
            {"job_id": str(row["id"])},
        )

    if len(artifacts) == 1:
        only = artifacts[0]
        return FileResponse(
            _artifact_path(row["id"], only["file"]),
            media_type=only["type"],
            filename=only["file"],
        )

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for a in artifacts:
            archive.write(_artifact_path(row["id"], a["file"]), arcname=a["file"])
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
) -> dict[str, Any]:
    """Take multipart files and submit a job.

    Each file field becomes the parameter of the same name, carrying the
    upload id. Non-file fields are passed through as ordinary parameters.
    """
    async with db.pool().acquire() as conn:
        # Before the body is read, not after: writing the files first would let
        # an unknown workspace name orphan 200MB on disk for every request that
        # then 404s.
        await _manifest(conn, repo, ws, "data_upload")

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
            conn, repo, ws, params, idempotency_key=idempotency_key
        )
    response.headers["Location"] = f"/rest/v1/transformations/jobs/id/{job_id}"
    return {"id": str(job_id), "status": "queued", "files": received}


def main() -> int:
    import uvicorn

    uvicorn.run(app, host=config.HOST, port=config.PORT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
