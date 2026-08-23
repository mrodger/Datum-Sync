"""Server-sent job events, replayed from the log and then tailed live.

The ordering here is the whole point. A reader that fetches history and *then*
starts listening loses every event that lands in between -- for a fast job that
is most of them. So the listener goes on first, and the history read that
follows is deduplicated against it by `job_log.id`.

    LISTEN                 -- nothing after this instant can be missed
    read jobs.status       -- if already terminal, history is the whole story
    read job_log           -- establishes the cursor
    drain, skipping id <= cursor

Log events carry an SSE `id:`, so a browser reconnecting sends `Last-Event-ID`
and resumes from it. Status and progress events do not: status is recovered
from `jobs.status` on reconnect, and progress is not durable by design.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import uuid
from typing import Any, AsyncIterator

import asyncpg

from datum_sync import jobs

HEARTBEAT_SECONDS = 15.0

# Bounded because a client that stops reading must not grow this without
# limit. Overflow drops the oldest event; job_log is the durable copy, so a
# reader that cares can re-fetch. Progress and status are not durable anyway.
QUEUE_MAX = 1000


def _sse(event: str, data: dict[str, Any], event_id: int | None = None) -> str:
    head = f"id: {event_id}\n" if event_id is not None else ""
    return f"{head}event: {event}\ndata: {json.dumps(data)}\n\n"


def _status_event(row: asyncpg.Record) -> str:
    return _sse(
        "status",
        {
            "job_id": str(row["id"]),
            "status": row["status"],
            "error": row["error"],
            "artifacts": json.loads(row["artifacts"]),
        },
    )


async def job_events(
    conn: asyncpg.Connection, job_id: uuid.UUID, after_id: int = 0
) -> AsyncIterator[str]:
    """Yield SSE frames for one job until it reaches a terminal status."""
    queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=QUEUE_MAX)
    wanted = str(job_id)

    def on_notify(_conn, _pid, _channel, payload: str) -> None:
        try:
            body = json.loads(payload)
        except json.JSONDecodeError:
            return
        if body.get("job_id") != wanted:
            return
        if queue.full():
            with contextlib.suppress(asyncio.QueueEmpty):
                queue.get_nowait()
        queue.put_nowait(body)

    await conn.add_listener(jobs.CHANNEL, on_notify)
    try:
        row = await jobs.get(conn, job_id)
        if row is None:
            return
        yield _status_event(row)

        cursor = after_id
        for entry in await jobs.get_log(conn, job_id, after_id=cursor):
            cursor = entry["id"]
            yield _sse(
                "log",
                {"level": entry["level"], "message": entry["message"],
                 "ts": entry["ts"].isoformat()},
                event_id=cursor,
            )

        if row["status"] in jobs.TERMINAL:
            # Nothing further will ever be published for this job.
            return

        while True:
            try:
                body = await asyncio.wait_for(
                    queue.get(), timeout=HEARTBEAT_SECONDS
                )
            except asyncio.TimeoutError:
                # Idle connections are dropped by proxies; a comment frame is
                # not delivered to the client's handlers but keeps the socket.
                yield ": keepalive\n\n"
                continue

            kind = body.get("event")
            if kind == "log":
                log_id = body.get("id")
                if isinstance(log_id, int) and log_id <= cursor:
                    continue          # already replayed from job_log
                if isinstance(log_id, int):
                    cursor = log_id
                yield _sse(
                    "log",
                    {"level": body.get("level", "info"),
                     "message": body.get("message", "")},
                    event_id=log_id if isinstance(log_id, int) else None,
                )
            elif kind == "progress":
                yield _sse("progress", {"pct": body.get("pct"),
                                        "message": body.get("message", "")})
            elif kind == "status":
                if body.get("status") in jobs.TERMINAL:
                    # Re-read rather than trust the payload: artifacts are too
                    # large for a NOTIFY and are not in it.
                    final = await jobs.get(conn, job_id)
                    if final is not None:
                        yield _status_event(final)
                    return
                yield _sse("status", {"job_id": wanted,
                                      "status": body.get("status")})
    finally:
        with contextlib.suppress(Exception):
            await conn.remove_listener(jobs.CHANNEL, on_notify)
