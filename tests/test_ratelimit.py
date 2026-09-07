"""`service_accounts.rate_limit_per_min` is a number that does something.

The column was declared in 001_core.sql and read by no Python until C3. That is
the failure mode these tests are aimed at, and it is worth being precise about
what it looked like: nothing was broken. Every test passed, `psql` showed the
column, and an operator reading the schema would have concluded the server was
rate limited. A control that exists only in the schema is indistinguishable
from one that is implemented, right up until someone needs it.

So the tests below are split in two. The unit tests pin the window's arithmetic
-- the boundary, and what happens to a client that keeps hammering after it is
refused. The HTTP tests prove the arithmetic is actually reached on a real
request, which is the half that was missing before and the half that the
middleware ordering can silently take away again.
"""
from __future__ import annotations

import asyncpg
import httpx
import pytest
import pytest_asyncio

from datum_sync import db as db_module, ratelimit, tokens
from datum_sync.api import app

pytestmark = pytest.mark.asyncio

ACCOUNT_NAME = "_ratelimit_acct"
LIMIT = 3


@pytest.fixture(autouse=True)
def clean_window():
    """Module-level state, so it is cleared on both sides of every test.

    Cleared *after* as well as before because this dict is shared with the rest
    of the suite: a test here that left hits behind would rate-limit an
    unrelated test whose account happened to reuse the id.
    """
    ratelimit.reset()
    yield
    ratelimit.reset()


# -- the window ---------------------------------------------------------------


def test_the_limit_admits_exactly_the_number_set():
    """N requests pass, the N+1th does not.

    Off-by-one is the whole risk in a counter: a limit of 3 that admits 4 is
    still a working rate limit by every coarse test one might write, and it is
    wrong.

    Guard: RATE-001.
    """
    now = 1000.0
    for i in range(LIMIT):
        assert ratelimit.check(1, LIMIT, now + i) == 0, f"request {i + 1} refused"
    assert ratelimit.check(1, LIMIT, now + LIMIT) > 0


def test_no_limit_means_no_limit():
    """`None` is what every account that predates C3 has.

    Fail-open, and deliberately: the alternative is that deploying this change
    throttles every existing caller to a number nobody chose.
    """
    for i in range(500):
        assert ratelimit.check(2, None, 1000.0 + i) == 0


def test_a_refused_request_is_not_recorded():
    """Hammering a closed door does not extend how long it stays closed.

    A window that counted rejected requests would punish exactly the clients
    that retry, which is all of them: the refusals pile into the window and
    hold it shut long after the requests that filled it have aged out. The
    quoted `Retry-After` would then be a lie in the direction that matters --
    the client waits exactly as told and is refused again.

    Asserted as "honour the wait and get in", not as "the number stops
    growing". The number is computed from the *oldest* hit, so it does not
    grow under this bug and an assertion about it passes against the defect.

    Guard: RATE-002.
    """
    now = 1000.0
    for i in range(LIMIT):
        assert ratelimit.check(3, LIMIT, now + i) == 0

    wait = ratelimit.check(3, LIMIT, now + 10)
    assert wait > 0
    for _ in range(50):
        ratelimit.check(3, LIMIT, now + 10)

    assert ratelimit.check(3, LIMIT, now + 10 + wait) == 0


def test_the_window_slides():
    """Hits older than 60s stop counting, one at a time.

    Not a fixed bucket that empties on the minute: a caller at a limit of 3
    that spends its budget at 00:59 must not get another 3 at 01:00.
    """
    now = 1000.0
    for i in range(LIMIT):
        assert ratelimit.check(4, LIMIT, now + i) == 0
    assert ratelimit.check(4, LIMIT, now + 30) > 0

    # Hits went in at t, t+1, t+2. At t+60.5 the first has aged out and the
    # other two have not, so exactly one slot has opened -- not three.
    assert ratelimit.check(4, LIMIT, now + 60.5) == 0
    assert ratelimit.check(4, LIMIT, now + 60.5) > 0


def test_retry_after_is_at_least_a_second():
    """A client that honours the header is not refused for obeying it.

    Truncating toward zero would hand back `Retry-After: 0` for most of the
    last second of the window, which tells a well-behaved client to retry
    immediately into another refusal.
    """
    now = 1000.0
    for i in range(LIMIT):
        ratelimit.check(5, LIMIT, now)
    wait = ratelimit.check(5, LIMIT, now + 59.99)
    assert wait >= 1
    # And having waited it, the caller gets in.
    assert ratelimit.check(5, LIMIT, now + 59.99 + wait) == 0


def test_accounts_do_not_share_a_window():
    now = 1000.0
    for i in range(LIMIT):
        assert ratelimit.check(10, LIMIT, now) == 0
    assert ratelimit.check(10, LIMIT, now) > 0
    assert ratelimit.check(11, LIMIT, now) == 0


def test_the_key_count_is_bounded():
    """Expired keys are evicted once the dict is large.

    Bounded by the number of accounts rather than by anything a caller
    controls, so this is housekeeping -- but an unbounded dict on a
    long-running process is worth not having either way.
    """
    for account_id in range(ratelimit._MAX_KEYS + 200):
        ratelimit.check(account_id, LIMIT, 1000.0)
    # Every key above is expired by now, so the eviction pass can reclaim.
    ratelimit.check(999_999, LIMIT, 5000.0)
    assert len(ratelimit._hits) <= ratelimit._MAX_KEYS


def test_the_window_is_per_process():
    """A statement about the deployment, asserted so it cannot drift silently.

    The limit lives in a module-level dict, which is correct for one process
    and wrong for two: `--workers 4` would give each worker its own window and
    multiply every limit by four, with no error and no failing test. `api.py`
    calls `uvicorn.run(app, ...)` with no `workers` argument, so there is one.

    If this test ever fails, the fix is not to change the assertion -- it is
    that `ratelimit` now needs the `rate_windows` table that
    `spec/datum-gate/03-authority.md` §8 describes.
    """
    source = (
        __import__("pathlib").Path(__file__).resolve().parent.parent
        / "datum_sync" / "api.py"
    ).read_text()
    assert "workers=" not in source


# -- reaching it over HTTP ----------------------------------------------------


@pytest_asyncio.fixture
async def db():
    from datum_sync import config

    try:
        conn = await asyncpg.connect(config.DATABASE_URL, timeout=3)
    except Exception:
        pytest.skip("database unavailable")
    yield conn
    await conn.execute("DELETE FROM audit_log WHERE actor_name = $1", ACCOUNT_NAME)
    await conn.execute("DELETE FROM service_accounts WHERE name = $1", ACCOUNT_NAME)
    await conn.close()


@pytest_asyncio.fixture
async def limited(db):
    """An account with a limit of 3, and a token for it."""
    account_id = await db.fetchval(
        """
        INSERT INTO service_accounts (name, max_tier, is_admin, rate_limit_per_min)
        VALUES ($1, 4, true, $2) RETURNING id
        """,
        ACCOUNT_NAME,
        LIMIT,
    )
    _, raw = await tokens.create(db, account_id, "fixture")
    return raw


@pytest_asyncio.fixture
async def client(db):
    await db_module.init_pool()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as c:
        yield c
    await db_module.close_pool()


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def test_the_limit_is_enforced_over_http(client, limited):
    """Four requests, three answered, and the fourth carries Retry-After.

    This is the test the whole change exists to make pass, and it is also the
    one that catches the middleware being registered in the wrong place. The
    rate limiter has to run *inside* `authenticate`, which means being declared
    *before* it -- each `@app.middleware` inserts at index 0, so the first one
    written is the last one entered. Declared after `authenticate` instead, it
    runs before the principal is attached, `getattr` returns None, and the
    limiter waves every request through. Nothing raises. Only this goes red.

    Guard: RATE-003.

    Guard: RATE-004.
    """
    for i in range(LIMIT):
        r = await client.get("/rest/v1/whoami", headers=_auth(limited))
        assert r.status_code == 200, f"request {i + 1} was refused: {r.text}"

    r = await client.get("/rest/v1/whoami", headers=_auth(limited))
    assert r.status_code == 429
    assert r.json()["code"] == "RATE_LIMITED"
    assert int(r.headers["Retry-After"]) >= 1


async def test_the_limit_runs_inside_authentication():
    """Asserted directly as well, because the symptom above names no cause.

    `test_the_limit_is_enforced_over_http` going red says the limit does not
    work; it does not say why. This says why, in the one line that would have
    to change for it to happen.
    """
    names = [
        m.kwargs["dispatch"].__name__
        for m in app.user_middleware
        if "dispatch" in m.kwargs
    ]
    # Innermost is last in this list: index 0 is outermost.
    assert names.index("rate_limit") > names.index("authenticate")
    # ...and still inside the audit layer, so a 429 is a row.
    assert names.index("rate_limit") > names.index("trace_and_audit")


async def test_an_unlimited_account_is_not_limited(client, db):
    """The default. Every account that exists today has a NULL in this column."""
    account_id = await db.fetchval(
        """
        INSERT INTO service_accounts (name, max_tier, is_admin)
        VALUES ($1, 4, true) RETURNING id
        """,
        ACCOUNT_NAME,
    )
    _, raw = await tokens.create(db, account_id, "fixture")
    for _ in range(LIMIT + 3):
        r = await client.get("/rest/v1/whoami", headers=_auth(raw))
        assert r.status_code == 200


async def test_a_refused_request_is_still_audited(client, limited, db):
    """429 is not a hole in the audit trail.

    The limiter sits inside `trace_and_audit`, so a rejection travels back out
    through it like any other response. Asserted because the tempting
    alternative -- rejecting as early as possible, outside everything -- would
    make the requests an operator most wants to see the only ones with no row.
    """
    for _ in range(LIMIT + 1):
        await client.post("/rest/v1/repositories", headers=_auth(limited), json={})

    rows = await db.fetch(
        "SELECT error_code FROM audit_log WHERE actor_name = $1 ORDER BY id",
        ACCOUNT_NAME,
    )
    assert 429 in [r["error_code"] for r in rows]
