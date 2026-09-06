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

from datum_sync import auth, config, tokens
from datum_sync.worker import WORKER_LOCK

TEST_REPO = "_pytest"
TEST_ACCOUNT = "_pytest"


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

    # The reverse hazard: a running worker claims the jobs these tests submit
    # and fails them against the fixture's /nonexistent path, so a test
    # asserting on a job it queued sees 'failed' for reasons of its own making.
    # Read pg_locks rather than trying the lock -- taking it, even briefly,
    # would make a worker starting in that instant exit.
    worker = await conn.fetchval(
        """
        SELECT count(*) FROM pg_locks
        WHERE locktype = 'advisory' AND granted
          AND classid = $1 AND objid = $2
        """,
        WORKER_LOCK >> 32,
        WORKER_LOCK & 0xFFFF_FFFF,
    )
    if worker:
        await conn.close()
        pytest.skip("a worker is running; it would claim these tests' jobs")

    try:
        yield conn
    finally:
        # Jobs cascade to job_log and idempotency_keys; workspaces cascade
        # from the repository.
        await conn.execute("DELETE FROM jobs WHERE repository = $1", TEST_REPO)
        await conn.execute("DELETE FROM repositories WHERE name = $1", TEST_REPO)
        await conn.close()


@pytest_asyncio.fixture
async def token(db):
    """A service account token that reaches everything.

    Wildcard-scoped and admin on purpose: this fixture exists so the *other*
    tests are authenticated, not to test authorisation. A test that cares about
    scope narrows it itself, and the tests that care about a missing or wrong
    credential send their own -- see test_auth.py.

    `repo_scope` is stated rather than left out. It used to be omitted, because
    the column defaulted to NULL and NULL meant every repository; since
    migration 013 the default is `{}` and means none, so a fixture that says
    nothing would authenticate as a principal holding no repository at all --
    and the tests that use it would fail 403 far from the cause.
    """
    await db.execute("DELETE FROM service_accounts WHERE name = $1", TEST_ACCOUNT)
    account_id = await db.fetchval(
        """
        INSERT INTO service_accounts (name, max_tier, is_admin, repo_scope)
        VALUES ($1, 4, true, ARRAY['*'])
        RETURNING id
        """,
        TEST_ACCOUNT,
    )
    # Minted through `tokens.create` rather than a hand-written INSERT: the
    # fixture that authenticates almost every other test should use the same
    # path the CLI does, so a break in minting fails loudly here instead of
    # leaving the suite green against a credential no production code makes.
    _, raw = await tokens.create(db, account_id, "fixture")
    try:
        yield raw
    finally:
        # oauth_tokens/oauth_codes/account_tokens reference the account;
        # cascade takes them.
        await db.execute("DELETE FROM service_accounts WHERE name = $1", TEST_ACCOUNT)


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
        # job_submitter only: the API tests rely on this workspace *not*
        # publishing data_streaming, so the service gate has something real to
        # refuse.
        "services": ["job_submitter"],
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


@pytest.fixture(autouse=True)
def _clear_password_attempts():
    """Reset the login rate limiter between tests.

    `auth._attempts` is module-level state shared by every test in the process,
    so without this a test that submits a wrong password leaves a failure behind
    and the lockout tests pass or fail according to collection order. Autouse
    because the coupling is invisible at the call site -- a test does not have
    to mention the limiter to be affected by it.
    """
    auth.reset_attempts()
    yield
    auth.reset_attempts()
