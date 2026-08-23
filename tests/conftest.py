"""Shared fixtures.

The `db` fixture talks to the real Postgres rather than a mock. The parts of
the job engine most worth testing -- SKIP LOCKED claim behaviour, ON CONFLICT
under concurrency, cascade deletes -- are properties of Postgres, and a mock
would assert only that the code calls the functions it calls.
"""
from __future__ import annotations

import json

import asyncpg
import pytest
import pytest_asyncio

from datum_sync import config

TEST_REPO = "_pytest"


@pytest_asyncio.fixture
async def db():
    try:
        conn = await asyncpg.connect(config.DATABASE_URL, timeout=3)
    except (OSError, asyncpg.PostgresError):
        pytest.skip("database unavailable")

    # claim() and requeue_orphans() act on the whole queue -- they have to,
    # that is what a queue is. So these tests are isolated only while no real
    # job is in flight: otherwise a drain loop here claims someone else's job
    # and abandons it in 'running'. Refuse rather than corrupt.
    busy = await conn.fetchval(
        """
        SELECT count(*) FROM jobs
        WHERE status IN ('queued', 'running') AND repository <> $1
        """,
        TEST_REPO,
    )
    if busy:
        await conn.close()
        pytest.skip(f"{busy} live job(s) in the queue; refusing to disturb them")

    try:
        yield conn
    finally:
        # Jobs cascade to job_log and idempotency_keys; workspaces cascade
        # from the repository.
        await conn.execute("DELETE FROM jobs WHERE repository = $1", TEST_REPO)
        await conn.execute("DELETE FROM repositories WHERE name = $1", TEST_REPO)
        await conn.close()


@pytest_asyncio.fixture
async def workspace(db):
    """A published workspace in a throwaway repository."""
    manifest = {
        "name": "fixture",
        "version": "1.0.0",
        "parameters": [
            {"name": "WHO", "type": "STRING", "required": True},
            {"name": "LOUD", "type": "BOOLEAN", "default": False},
        ],
        "outputs": [{"name": "out", "type": "text/plain", "primary": True}],
        "timeout_seconds": 20,
    }
    repo_id = await db.fetchval(
        "INSERT INTO repositories (name, path) VALUES ($1, $2) RETURNING id",
        TEST_REPO,
        "/nonexistent",
    )
    await db.execute(
        """
        INSERT INTO workspaces (repository_id, name, version, manifest)
        VALUES ($1, 'fixture', '1.0.0', $2)
        """,
        repo_id,
        json.dumps(manifest),
    )
    return TEST_REPO, "fixture"
