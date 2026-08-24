"""Worker pool. Claims queued jobs and runs them.

    python -m datum_sync.worker              run until interrupted
    python -m datum_sync.worker --once       drain the queue and exit

Two things wake a worker: a NOTIFY on `job_events`, and a poll timer. The
timer is not a fallback for a flaky NOTIFY -- it is the primary correctness
mechanism. A job submitted while the worker was down, restarting, or between
LISTEN calls generates a notification nobody receives, and Postgres does not
replay it. The poll is what guarantees such a job still runs.

The same poll asks whether any schedule is due, which is why there is no
scheduler process and no timer: see datum_sync/schedules.py. It also asks which
finished jobs no automation has considered yet, for the same reason the poll
exists at all -- see datum_sync/automations.py.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import signal
import sys
import uuid
from typing import Any

import asyncpg

from datum_sync import (
    automations, config, connections, crypto, jobs, schedules, uploads,
)
from datum_sync.runner import Runner

POLL_SECONDS = 5.0
DEFAULT_CONCURRENCY = 4

# Advisory lock key. Only one worker process may run against a database,
# because requeue_orphans() assumes every 'running' row belongs to a dead
# predecessor. Refusing the second process is safer than documenting the rule.
WORKER_LOCK = 0x0DA7_0000


class Worker:
    def __init__(self, concurrency: int = DEFAULT_CONCURRENCY) -> None:
        self.concurrency = concurrency
        self.stopping = asyncio.Event()
        self.wake = asyncio.Event()
        self._active: dict[uuid.UUID, Runner] = {}
        self._pool: asyncpg.Pool | None = None

    # -- lifecycle ---------------------------------------------------------

    async def run(self, once: bool = False) -> None:
        self._pool = await asyncpg.create_pool(config.DATABASE_URL, min_size=2,
                                               max_size=self.concurrency + 3)
        assert self._pool is not None
        lock_conn = await self._pool.acquire()
        try:
            if not await lock_conn.fetchval("SELECT pg_try_advisory_lock($1)",
                                            WORKER_LOCK):
                raise SystemExit(
                    "another datum-sync worker holds the lock on this database"
                )

            async with self._pool.acquire() as conn:
                n = await jobs.requeue_orphans(conn)
                if n:
                    print(f"requeued {n} orphaned job(s) from a previous run")

            listener = await self._pool.acquire()
            await listener.add_listener(jobs.CHANNEL, self._on_event)
            await listener.add_listener(jobs.CONTROL_CHANNEL, self._on_control)
            try:
                await self._loop(once=once)
            finally:
                await listener.remove_listener(jobs.CHANNEL, self._on_event)
                await listener.remove_listener(jobs.CONTROL_CHANNEL, self._on_control)
                await self._pool.release(listener)
        finally:
            await lock_conn.execute("SELECT pg_advisory_unlock($1)", WORKER_LOCK)
            await self._pool.release(lock_conn)
            await self._pool.close()

    async def _loop(self, once: bool) -> None:
        running: set[asyncio.Task] = set()

        while not self.stopping.is_set():
            await self._tick_schedules()
            await self._tick_automations()

            while len(running) < self.concurrency:
                job = await self._claim()
                if job is None:
                    break
                task = asyncio.create_task(self._execute(job))
                running.add(task)
                task.add_done_callback(running.discard)

            if once and not running:
                return

            self.wake.clear()
            waiters = [asyncio.create_task(self.wake.wait())]
            if running:
                waiters.append(asyncio.create_task(asyncio.wait(running,
                    return_when=asyncio.FIRST_COMPLETED)))
            done, pending = await asyncio.wait(
                waiters, timeout=POLL_SECONDS, return_when=asyncio.FIRST_COMPLETED
            )
            for t in pending:
                t.cancel()

        if running:
            await asyncio.gather(*running, return_exceptions=True)

    def _on_event(self, _conn, _pid, _channel, payload: str) -> None:
        try:
            if json.loads(payload).get("status") == "queued":
                self.wake.set()
        except (json.JSONDecodeError, AttributeError):
            self.wake.set()

    def _on_control(self, _conn, _pid, _channel, payload: str) -> None:
        try:
            body = json.loads(payload)
            job_id = uuid.UUID(body["job_id"])
        except (json.JSONDecodeError, KeyError, ValueError):
            return
        runner = self._active.get(job_id)
        if runner is not None and body.get("action") == "cancel":
            asyncio.create_task(runner.cancel())

    def stop(self) -> None:
        self.stopping.set()
        self.wake.set()

    # -- schedules ---------------------------------------------------------

    async def _tick_schedules(self) -> None:
        """Submit anything due. Runs on the same poll that claims jobs.

        There is no timer and no scheduler object: `schedules.next_run` is the
        due time, and this asks the database which rows have passed it. The
        resolution is therefore the poll interval, which is the trade made for
        having one source of truth that an API edit updates directly.

        Failures are printed and swallowed. A schedule pointing at a workspace
        someone unpublished is an operational fact about that schedule; taking
        the worker down over it would stop every unrelated job as well.
        """
        assert self._pool is not None
        try:
            async with self._pool.acquire() as conn:
                fired = await schedules.run_due(conn)
        except Exception as exc:  # noqa: BLE001 - a bad schedule is not fatal
            print(f"schedule tick failed: {type(exc).__name__}: {exc}", flush=True)
            return

        for entry in fired:
            if entry["error"]:
                print(f"schedule {entry['schedule']!r} did not submit: "
                      f"{entry['error']}", flush=True)
            else:
                print(f"schedule {entry['schedule']!r} submitted job "
                      f"{entry['job_id']}", flush=True)

    async def _tick_automations(self) -> None:
        """Consider every finished job no automation has seen yet.

        Driven off a column rather than off the completion NOTIFY. A job that
        finishes while this worker is restarting still gets considered, just
        late -- `automations_at IS NULL` stays true until some worker acts on
        it. That does mean the last jobs to finish under `--once` are left for
        the next run, which is the correct end of that trade: late is a delay,
        lost is a delivery that never happened and nothing to say so.

        Swallows for the same reason `_tick_schedules` does, with one addition:
        an action here makes an outbound HTTP request, so the failure modes
        include every way a third-party endpoint can be broken.
        """
        assert self._pool is not None
        try:
            async with self._pool.acquire() as conn:
                fired = await automations.run_pending(conn)
        except Exception as exc:  # noqa: BLE001 - a bad automation is not fatal
            print(f"automation tick failed: {type(exc).__name__}: {exc}", flush=True)
            return

        for entry in fired:
            state = "fired" if entry["ok"] else "fired with errors"
            print(f"automation {entry['automation']!r} {state} on job "
                  f"{entry['job_id']}", flush=True)

    # -- execution ---------------------------------------------------------

    async def _claim(self) -> asyncpg.Record | None:
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            return await jobs.claim(conn)

    async def _execute(self, job: asyncpg.Record) -> None:
        assert self._pool is not None
        job_id = job["id"]
        async with self._pool.acquire() as conn:
            try:
                manifest = await jobs.load_manifest_for(
                    conn, job["repository"], job["workspace"]
                )
            except jobs.JobError as e:
                # The workspace was unpublished between submit and claim.
                await jobs.finish(conn, job_id, "failed", error=str(e))
                return

            try:
                params = uploads.resolve_params(
                    manifest, json.loads(job["params"])
                )
            except uploads.UploadError as e:
                # The upload was swept between submit and claim.
                await jobs.finish(conn, job_id, "failed", error=str(e))
                return

            try:
                resolved = await connections.resolve(
                    conn,
                    job["repository"],
                    job["workspace"],
                    [c.name for c in manifest.connections],
                )
            except (connections.ConnectionStoreError, crypto.CryptoError) as e:
                # A declared connection was deleted, moved out of scope, or
                # cannot be decrypted. Failing here rather than passing a short
                # dict means the job's error names the connection; the
                # alternative is a KeyError from inside the workspace that says
                # only that some key was missing.
                await jobs.finish(conn, job_id, "failed", error=str(e))
                return

            ws_path = (
                config.REPOSITORIES_PATH / job["repository"] / job["workspace"]
            )
            artifact_dir = config.DATA_PATH / "jobs" / str(job_id)

            async def sink(event_type: str, payload: dict[str, Any]) -> None:
                await self._sink(conn, job_id, event_type, payload)

            runner = Runner(
                workspace_path=ws_path,
                manifest=manifest,
                params=params,
                artifact_dir=artifact_dir,
                sink=sink,
                connections=resolved,
            )
            self._active[job_id] = runner
            try:
                result = await runner.run()
            except Exception as e:
                await jobs.finish(
                    conn, job_id, "failed",
                    error=f"runner error: {type(e).__name__}: {e}",
                )
                return
            finally:
                self._active.pop(job_id, None)

            await jobs.finish(
                conn, job_id, result.status, result.artifacts, result.error
            )

    async def _sink(
        self,
        conn: asyncpg.Connection,
        job_id: uuid.UUID,
        event_type: str,
        payload: dict[str, Any],
    ) -> None:
        if event_type == "progress":
            await jobs.progress(
                conn, job_id, float(payload.get("pct", 0.0)),
                str(payload.get("message", "")),
            )
        else:
            await jobs.log(
                conn,
                job_id,
                str(payload.get("message", json.dumps(payload))),
                level=str(payload.get("level", "info")),
            )


async def _amain(once: bool, concurrency: int) -> None:
    worker = Worker(concurrency=concurrency)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, worker.stop)
    await worker.run(once=once)


def main() -> int:
    parser = argparse.ArgumentParser(prog="datum_sync.worker")
    parser.add_argument("--once", action="store_true",
                        help="drain the queue and exit")
    parser.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY)
    args = parser.parse_args()
    asyncio.run(_amain(args.once, args.concurrency))
    return 0


if __name__ == "__main__":
    sys.exit(main())
