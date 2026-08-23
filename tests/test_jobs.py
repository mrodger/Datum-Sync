"""Job record tests against a real Postgres."""
from __future__ import annotations

import asyncio
import json

import asyncpg
import pytest

from datum_sync import config, jobs

pytestmark = pytest.mark.asyncio


# -- submit ----------------------------------------------------------------


async def test_submit_validates_and_applies_defaults(db, workspace):
    repo, ws = workspace
    job_id = await jobs.submit(db, repo, ws, {"WHO": "simone", "LOUD": "yes"})
    row = await db.fetchrow("SELECT status, params FROM jobs WHERE id = $1", job_id)
    assert row["status"] == "queued"
    assert json.loads(row["params"]) == {"WHO": "simone", "LOUD": True}


async def test_submit_rejects_unknown_parameter(db, workspace):
    repo, ws = workspace
    with pytest.raises(jobs.JobError, match="unknown parameter"):
        await jobs.submit(db, repo, ws, {"WHO": "x", "WHOM": "y"})


async def test_submit_rejects_missing_required(db, workspace):
    repo, ws = workspace
    with pytest.raises(jobs.JobError, match="missing required"):
        await jobs.submit(db, repo, ws, {})


async def test_submit_rejects_unpublished_workspace(db):
    with pytest.raises(jobs.JobError, match="no published workspace"):
        await jobs.submit(db, "_pytest", "ghost", {})


async def test_rejected_submit_queues_nothing(db, workspace):
    """A validation failure must not leave a row behind."""
    repo, ws = workspace
    before = await db.fetchval("SELECT count(*) FROM jobs WHERE repository = $1", repo)
    with pytest.raises(jobs.JobError):
        await jobs.submit(db, repo, ws, {"WHO": "x", "BAD": 1})
    after = await db.fetchval("SELECT count(*) FROM jobs WHERE repository = $1", repo)
    assert after == before


# -- idempotency -----------------------------------------------------------


async def test_idempotency_key_returns_the_same_job(db, workspace):
    repo, ws = workspace
    a = await jobs.submit(db, repo, ws, {"WHO": "x"}, submitted_by="marcus",
                          idempotency_key="k1")
    b = await jobs.submit(db, repo, ws, {"WHO": "x"}, submitted_by="marcus",
                          idempotency_key="k1")
    assert a == b
    assert await db.fetchval(
        "SELECT count(*) FROM jobs WHERE repository = $1", repo
    ) == 1


async def test_idempotency_key_is_scoped_per_account(db, workspace):
    """Two accounts using the same key get their own jobs."""
    repo, ws = workspace
    a = await jobs.submit(db, repo, ws, {"WHO": "x"}, submitted_by="simone",
                          idempotency_key="shared")
    b = await jobs.submit(db, repo, ws, {"WHO": "x"}, submitted_by="robert",
                          idempotency_key="shared")
    assert a != b


async def test_concurrent_submits_with_one_key_produce_one_job(db, workspace):
    """The losing transaction rolls back its job rather than orphaning it."""
    repo, ws = workspace

    async def submit_once():
        conn = await asyncpg.connect(config.DATABASE_URL)
        try:
            return await jobs.submit_idempotent(
                conn, repo, ws, {"WHO": "x"},
                submitted_by="marcus", idempotency_key="race",
            )
        finally:
            await conn.close()

    results = await asyncio.gather(*(submit_once() for _ in range(5)))
    assert len(set(results)) == 1
    assert await db.fetchval(
        "SELECT count(*) FROM jobs WHERE repository = $1", repo
    ) == 1


# -- claim -----------------------------------------------------------------


async def test_claim_marks_running(db, workspace):
    repo, ws = workspace
    job_id = await jobs.submit(db, repo, ws, {"WHO": "x"})
    claimed = await jobs.claim(db)
    assert claimed["id"] == job_id
    assert await db.fetchval("SELECT status FROM jobs WHERE id = $1", job_id) == "running"


async def test_claim_returns_none_when_queue_is_empty(db, workspace):
    while await jobs.claim(db):
        pass
    assert await jobs.claim(db) is None


async def test_two_workers_never_claim_the_same_job(db, workspace):
    """FOR UPDATE SKIP LOCKED is what makes the pool safe."""
    repo, ws = workspace
    while await jobs.claim(db):
        pass
    submitted = [await jobs.submit(db, repo, ws, {"WHO": str(i)}) for i in range(6)]

    async def claim_all():
        conn = await asyncpg.connect(config.DATABASE_URL)
        got = []
        try:
            while (row := await jobs.claim(conn)) is not None:
                got.append(row["id"])
            return got
        finally:
            await conn.close()

    batches = await asyncio.gather(*(claim_all() for _ in range(4)))
    claimed = [j for b in batches for j in b]
    assert sorted(claimed) == sorted(submitted)
    assert len(claimed) == len(set(claimed))


async def test_claim_ignores_cancelled_jobs(db, workspace):
    repo, ws = workspace
    while await jobs.claim(db):
        pass
    job_id = await jobs.submit(db, repo, ws, {"WHO": "x"})
    assert await jobs.cancel(db, job_id) == "cancelled"
    assert await jobs.claim(db) is None


# -- finish and cancel -----------------------------------------------------


async def test_finish_rejects_a_non_terminal_status(db, workspace):
    repo, ws = workspace
    job_id = await jobs.submit(db, repo, ws, {"WHO": "x"})
    with pytest.raises(ValueError, match="not a terminal status"):
        await jobs.finish(db, job_id, "running")


async def test_cancel_of_a_finished_job_reports_its_status(db, workspace):
    repo, ws = workspace
    job_id = await jobs.submit(db, repo, ws, {"WHO": "x"})
    await jobs.finish(db, job_id, "complete")
    assert await jobs.cancel(db, job_id) == "complete"


async def test_cancel_of_a_running_job_signals_the_worker(db, workspace):
    repo, ws = workspace
    job_id = await jobs.submit(db, repo, ws, {"WHO": "x"})
    await jobs.claim(db)
    assert await jobs.cancel(db, job_id) == "signalled"


async def test_cancel_of_an_unknown_job_raises(db):
    import uuid
    with pytest.raises(jobs.JobError, match="no such job"):
        await jobs.cancel(db, uuid.uuid4())


# -- logging ---------------------------------------------------------------


async def test_log_is_readable_back(db, workspace):
    repo, ws = workspace
    job_id = await jobs.submit(db, repo, ws, {"WHO": "x"})
    await jobs.log(db, job_id, "first")
    await jobs.log(db, job_id, "second", level="warn")
    entries = await jobs.get_log(db, job_id)
    assert [e["message"] for e in entries] == ["first", "second"]
    assert entries[1]["level"] == "warn"


async def test_get_log_after_id_supports_resume(db, workspace):
    """An SSE client reconnecting replays only what it missed."""
    repo, ws = workspace
    job_id = await jobs.submit(db, repo, ws, {"WHO": "x"})
    await jobs.log(db, job_id, "one")
    await jobs.log(db, job_id, "two")
    first = await jobs.get_log(db, job_id)
    rest = await jobs.get_log(db, job_id, after_id=first[0]["id"])
    assert [e["message"] for e in rest] == ["two"]


async def test_unknown_log_level_falls_back_to_info(db, workspace):
    """The CHECK constraint would otherwise abort the run over a typo."""
    repo, ws = workspace
    job_id = await jobs.submit(db, repo, ws, {"WHO": "x"})
    await jobs.log(db, job_id, "msg", level="LOUD")
    assert (await jobs.get_log(db, job_id))[0]["level"] == "info"


async def test_oversized_notify_payload_does_not_raise(db, workspace):
    """Postgres caps a NOTIFY payload at 8000 bytes."""
    repo, ws = workspace
    job_id = await jobs.submit(db, repo, ws, {"WHO": "x"})
    await jobs.log(db, job_id, "x" * 20000)
    stored = (await jobs.get_log(db, job_id))[0]["message"]
    assert len(stored) == 20000, "the log row keeps the full message"


# -- orphan recovery -------------------------------------------------------


async def test_requeue_orphans_returns_running_jobs_to_the_queue(db, workspace):
    repo, ws = workspace
    while await jobs.claim(db):
        pass
    job_id = await jobs.submit(db, repo, ws, {"WHO": "x"})
    await jobs.claim(db)
    assert await db.fetchval("SELECT status FROM jobs WHERE id = $1", job_id) == "running"

    assert await jobs.requeue_orphans(db) >= 1
    row = await db.fetchrow("SELECT status, started_at FROM jobs WHERE id = $1", job_id)
    assert row["status"] == "queued"
    assert row["started_at"] is None


async def test_requeue_orphans_leaves_terminal_jobs_alone(db, workspace):
    repo, ws = workspace
    job_id = await jobs.submit(db, repo, ws, {"WHO": "x"})
    await jobs.finish(db, job_id, "complete")
    await jobs.requeue_orphans(db)
    assert await db.fetchval("SELECT status FROM jobs WHERE id = $1", job_id) == "complete"
