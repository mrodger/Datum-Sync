"""Job records: submit, claim, log, finish.

All state lives in Postgres. pg_notify on `job_events` is a wake-up hint for
workers and a live feed for SSE -- it is never the system of record. NOTIFY
payloads are dropped if no session is listening and are capped at 8000 bytes,
so anything that must survive a restart is written to a table first and
announced second.

Progress is the one deliberate exception: it is announced but not stored. A
run that reports every percent would otherwise write hundreds of job_log rows
per job to say nothing durable. A client that reconnects mid-run recovers
`jobs.status` and the full `job_log`, not the progress bar.
"""
from __future__ import annotations

import json
import uuid
from typing import Any

import asyncpg

from datum_sync.manifest import Manifest, ParameterError, validate_params

CHANNEL = "job_events"
CONTROL_CHANNEL = "job_control"

# NOTIFY payloads are capped at 8000 bytes by Postgres. Truncate well short of
# it so a long log line degrades instead of raising and killing the run.
_MAX_NOTIFY_TEXT = 2000

TERMINAL = ("complete", "failed", "cancelled")


class JobError(Exception):
    """Job cannot be submitted as asked."""


class WorkspaceNotFound(JobError):
    """No published workspace by that name.

    Separate from JobError so the API can answer 404 rather than 400 without
    matching on the message text.
    """


def _truncate(text: str) -> str:
    if len(text) <= _MAX_NOTIFY_TEXT:
        return text
    return text[:_MAX_NOTIFY_TEXT] + "...[truncated]"


async def notify(conn: asyncpg.Connection, job_id: uuid.UUID, **payload: Any) -> None:
    body = {"job_id": str(job_id), **payload}
    if isinstance(body.get("message"), str):
        body["message"] = _truncate(body["message"])
    await conn.execute("SELECT pg_notify($1, $2)", CHANNEL, json.dumps(body))


async def load_manifest_for(
    conn: asyncpg.Connection, repository: str, workspace: str
) -> Manifest:
    """Read the published manifest from the database, not from disk.

    An edit to manifest.json on disk must not change the interface of an
    already-published workspace; only a re-sync may do that.
    """
    row = await conn.fetchrow(
        """
        SELECT w.manifest
        FROM workspaces w JOIN repositories r ON r.id = w.repository_id
        WHERE r.name = $1 AND w.name = $2
        """,
        repository,
        workspace,
    )
    if row is None:
        raise WorkspaceNotFound(f"no published workspace {repository}/{workspace}")
    return Manifest.model_validate(json.loads(row["manifest"]))


async def submit(
    conn: asyncpg.Connection,
    repository: str,
    workspace: str,
    params: dict[str, Any],
    submitted_by: str | None = None,
    idempotency_key: str | None = None,
    parent_job: uuid.UUID | None = None,
) -> uuid.UUID:
    """Queue a job. Returns its id.

    Parameters are validated here rather than in the worker so a caller gets a
    synchronous rejection for a bad request instead of a job that queues
    successfully and then fails a second later for a reason nobody saw.
    """
    manifest = await load_manifest_for(conn, repository, workspace)
    try:
        validated = validate_params(manifest, params)
    except ParameterError as e:
        raise JobError(str(e)) from None

    if idempotency_key is not None:
        existing = await _existing_for_key(conn, idempotency_key, submitted_by)
        if existing is not None:
            return existing

    async with conn.transaction():
        job_id = await conn.fetchval(
            """
            INSERT INTO jobs (repository, workspace, params, submitted_by, parent_job)
            VALUES ($1, $2, $3, $4, $5)
            RETURNING id
            """,
            repository,
            workspace,
            json.dumps(validated),
            submitted_by,
            parent_job,
        )
        if idempotency_key is not None:
            # ON CONFLICT blocks on a concurrent uncommitted insert of the same
            # key, then returns no row -- so a lost race is detected here, and
            # rolling back this transaction removes the job we just created.
            claimed = await conn.fetchval(
                """
                INSERT INTO idempotency_keys (key, account, job_id)
                VALUES ($1, $2, $3)
                ON CONFLICT (key, account) DO NOTHING
                RETURNING job_id
                """,
                idempotency_key,
                submitted_by or "",
                job_id,
            )
            if claimed is None:
                raise _KeyRace()

    await notify(conn, job_id, event="status", status="queued")
    return job_id


class _KeyRace(Exception):
    """Another submission claimed this idempotency key first."""


async def _existing_for_key(
    conn: asyncpg.Connection, key: str, account: str | None
) -> uuid.UUID | None:
    return await conn.fetchval(
        "SELECT job_id FROM idempotency_keys WHERE key = $1 AND account = $2",
        key,
        account or "",
    )


async def submit_idempotent(
    conn: asyncpg.Connection, *args: Any, **kwargs: Any
) -> uuid.UUID:
    """submit(), resolving a lost idempotency-key race to the winner's job."""
    key = kwargs.get("idempotency_key")
    try:
        return await submit(conn, *args, **kwargs)
    except _KeyRace:
        existing = await _existing_for_key(conn, key, kwargs.get("submitted_by"))
        if existing is None:  # pragma: no cover -- winner rolled back
            raise JobError("idempotency key race could not be resolved") from None
        return existing


async def claim(conn: asyncpg.Connection) -> asyncpg.Record | None:
    """Take the oldest queued job, or None.

    FOR UPDATE SKIP LOCKED is what makes the worker pool safe: two workers
    polling at the same instant take different rows rather than both taking the
    oldest. The UPDATE and the SELECT share one transaction, so a worker that
    dies between them leaves the job queued rather than running-forever.
    """
    async with conn.transaction():
        row = await conn.fetchrow(
            """
            SELECT id FROM jobs
            WHERE status = 'queued'
            ORDER BY submitted_at
            FOR UPDATE SKIP LOCKED
            LIMIT 1
            """
        )
        if row is None:
            return None
        return await conn.fetchrow(
            """
            UPDATE jobs SET status = 'running', started_at = now()
            WHERE id = $1
            RETURNING id, repository, workspace, params
            """,
            row["id"],
        )


async def log(
    conn: asyncpg.Connection,
    job_id: uuid.UUID,
    message: str,
    level: str = "info",
) -> None:
    if level not in ("debug", "info", "warn", "error"):
        level = "info"
    # The row id travels in the notification so an SSE reader can tell a
    # notification it already replayed from job_log from a new one. Without it
    # the reader has to choose between dropping events and duplicating them:
    # it must LISTEN before reading history (or it loses whatever lands in
    # between), which means the first notifications it sees are usually ones
    # the history read also returns.
    log_id = await conn.fetchval(
        """
        INSERT INTO job_log (job_id, level, message)
        VALUES ($1, $2, $3) RETURNING id
        """,
        job_id,
        level,
        message,
    )
    await notify(conn, job_id, event="log", id=log_id, level=level, message=message)


async def progress(
    conn: asyncpg.Connection, job_id: uuid.UUID, pct: float, message: str = ""
) -> None:
    await notify(conn, job_id, event="progress", pct=pct, message=message)


async def finish(
    conn: asyncpg.Connection,
    job_id: uuid.UUID,
    status: str,
    artifacts: list[dict[str, Any]] | None = None,
    error: str | None = None,
) -> None:
    if status not in TERMINAL:
        raise ValueError(f"not a terminal status: {status}")
    await conn.execute(
        """
        UPDATE jobs
        SET status = $2, completed_at = now(), artifacts = $3, error = $4
        WHERE id = $1
        """,
        job_id,
        status,
        json.dumps(artifacts or []),
        error,
    )
    await notify(conn, job_id, event="status", status=status, error=error)


async def cancel(conn: asyncpg.Connection, job_id: uuid.UUID) -> str:
    """Cancel a job. Returns what happened: cancelled | signalled | <status>.

    A queued job is cancelled by flipping the row -- claim() only reads
    status='queued', so it will never be picked up. A running job is owned by a
    worker process that holds the child's handle, so all this can do is ask:
    the worker listening on job_control does the killing.
    """
    row = await conn.fetchrow("SELECT status FROM jobs WHERE id = $1", job_id)
    if row is None:
        raise JobError(f"no such job {job_id}")

    if row["status"] == "queued":
        updated = await conn.fetchval(
            """
            UPDATE jobs SET status = 'cancelled', completed_at = now()
            WHERE id = $1 AND status = 'queued'
            RETURNING id
            """,
            job_id,
        )
        if updated is not None:
            await notify(conn, job_id, event="status", status="cancelled")
            return "cancelled"
        # Lost the race to a worker; fall through and signal instead.
        row = await conn.fetchrow("SELECT status FROM jobs WHERE id = $1", job_id)

    if row["status"] == "running":
        await conn.execute(
            "SELECT pg_notify($1, $2)",
            CONTROL_CHANNEL,
            json.dumps({"job_id": str(job_id), "action": "cancel"}),
        )
        return "signalled"

    return row["status"]


async def get(conn: asyncpg.Connection, job_id: uuid.UUID) -> asyncpg.Record | None:
    return await conn.fetchrow("SELECT * FROM jobs WHERE id = $1", job_id)


async def get_log(
    conn: asyncpg.Connection, job_id: uuid.UUID, after_id: int = 0
) -> list[asyncpg.Record]:
    return await conn.fetch(
        """
        SELECT id, ts, level, message FROM job_log
        WHERE job_id = $1 AND id > $2 ORDER BY id
        """,
        job_id,
        after_id,
    )


async def requeue_orphans(conn: asyncpg.Connection) -> int:
    """Return jobs left 'running' by a worker that died back to the queue.

    Called at worker startup. A crashed worker's children died with it, so the
    row is a lie -- nothing is running. Without this the job sticks in a status
    no timeout will ever clear.

    Correct only because a single worker process may run at a time: it takes
    WORKER_LOCK before calling this, so every 'running' row necessarily belongs
    to a dead predecessor. If the pool ever spans processes this must become a
    per-worker query keyed on a claimed_by column, or a starting worker will
    requeue a live worker's jobs out from under it.
    """
    rows = await conn.fetch(
        """
        UPDATE jobs SET status = 'queued', started_at = NULL
        WHERE status = 'running'
        RETURNING id
        """
    )
    for r in rows:
        await log(conn, r["id"], "worker restarted; job requeued", level="warn")
    return len(rows)
