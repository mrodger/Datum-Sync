"""Submit a workspace and wait for its result.

Extracted from `api.py` when the MCP endpoint became a second caller. The
alternative was for `mcp.py` to grow its own copy of `await_job`, and that
function is exactly the wrong thing to duplicate: it has to LISTEN before it
reads the job's status, or a job that finishes between the read and the
subscription never wakes it and the caller hangs until the timeout. A second
copy is a second chance to get that ordering wrong.

Nothing here executes a workspace itself. Every path submits to the same queue
the async endpoint uses, so the worker's concurrency limit, timeout,
cancellation and one-child-per-job guarantees hold no matter which door the
request came through.
"""
from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path
from typing import Any

import asyncpg

from datum_sync import config, db, jobs
from datum_sync.errors import ApiError
from datum_sync.manifest import Manifest
from datum_sync.worker import WORKER_LOCK

# Beyond the workspace's own timeout the job is being killed anyway; the extra
# margin covers the claim delay and the finish write.
SYNC_MARGIN_SECONDS = 15.0


async def job_row(conn: asyncpg.Connection, job_id: uuid.UUID) -> asyncpg.Record:
    row = await jobs.get(conn, job_id)
    if row is None:
        raise ApiError(404, "NOT_FOUND", f"no such job {job_id}")
    return row


async def manifest_for(
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


async def worker_is_running(conn: asyncpg.Connection) -> bool:
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


async def await_job(job_id: uuid.UUID, timeout: float) -> asyncpg.Record:
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
        row = await job_row(conn, job_id)
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
        return await job_row(conn, job_id)
    finally:
        await conn.close()


def artifact_path(job_id: uuid.UUID, filename: str) -> Path:
    """Resolve a stored artifact, refusing anything outside the job's dir."""
    root = (config.DATA_PATH / "jobs" / str(job_id)).resolve()
    path = (root / filename).resolve()
    if not path.is_file() or root not in path.parents:
        raise ApiError(404, "NOT_FOUND", f"artifact file missing: {filename}")
    return path


async def run_sync(
    repo: str,
    ws: str,
    params: dict[str, Any],
    service: str,
    submitted_by: str | None = None,
) -> tuple[asyncpg.Record, Manifest]:
    """Submit and wait. Shared by /stream, /download and MCP tools/call."""
    async with db.pool().acquire() as conn:
        manifest = await manifest_for(conn, repo, ws, service)
        if not await worker_is_running(conn):
            raise ApiError(
                503,
                "NO_WORKER",
                "no worker is running; the job would queue indefinitely",
            )
        job_id = await jobs.submit(conn, repo, ws, params, submitted_by=submitted_by)

    row = await await_job(job_id, manifest.timeout_seconds + SYNC_MARGIN_SECONDS)
    if row["status"] != "complete":
        raise ApiError(
            502,
            "JOB_FAILED",
            row["error"] or f"job {row['status']}",
            {"job_id": str(job_id), "status": row["status"]},
        )
    return row, manifest
