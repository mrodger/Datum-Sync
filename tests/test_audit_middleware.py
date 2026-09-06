"""Every write request is audited, including routes nobody remembered to wire.

The gap this closes is not "some routes were missing". Before this change
`audit.Trace.mint` had exactly one call site in the whole package -- the `/mcp`
endpoint -- so the REST API and the UI minted no trace and wrote no audit row at
all. One surface of three was covered, and `011_audit_log.sql` said so in a
comment that had been true for a release.

The property being proven is therefore about *construction*, not coverage today:
a route that no one thought about when writing the middleware is still audited.
A test that enumerated the current routes and checked each one would pass while
being exactly as forgettable as the thing it replaced -- so the central test here
registers a route the middleware has never heard of and POSTs to it.

The rest is the boundary: reads are not rows, `/mcp` keeps writing its own rows
and gains no duplicate, an unauthenticated request cannot be a row because
`actor_id` is NOT NULL, and a failure is recorded as one.
"""
from __future__ import annotations

import uuid

import asyncpg
import httpx
import pytest
import pytest_asyncio

from datum_sync import audit, db as db_module, tokens
from datum_sync.api import app

pytestmark = pytest.mark.asyncio

ACCOUNT_NAME = "_audit_mw_acct"

# Registered once, at import, and never mentioned in `datum_sync/api.py`. That
# is the entire point of it: the middleware has no route list, so a route it has
# never seen is the honest test of "audited by construction". Under `/rest/v1`
# so it is not in PUBLIC_PATHS and authenticates like everything else.
NEW_ROUTE = "/rest/v1/_audit_mw_probe"


@app.post(NEW_ROUTE, include_in_schema=False)
async def _audit_mw_probe() -> dict:
    return {"ok": True}


@pytest_asyncio.fixture
async def db():
    from datum_sync import config

    try:
        conn = await asyncpg.connect(config.DATABASE_URL, timeout=3)
    except Exception:
        pytest.skip("database unavailable")
    yield conn
    await conn.execute("DELETE FROM audit_log WHERE actor_name = $1", ACCOUNT_NAME)
    await conn.execute("DELETE FROM mcp_call_log WHERE account_name = $1", ACCOUNT_NAME)
    await conn.execute("DELETE FROM service_accounts WHERE name = $1", ACCOUNT_NAME)
    await conn.close()


@pytest_asyncio.fixture
async def token(db):
    account_id = await db.fetchval(
        """
        INSERT INTO service_accounts (name, max_tier, is_admin)
        VALUES ($1, 4, true) RETURNING id
        """,
        ACCOUNT_NAME,
    )
    _, account_token = await tokens.create(db, account_id, "fixture")
    return account_token


@pytest_asyncio.fixture
async def client(db):
    await db_module.init_pool()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as c:
        yield c
    await db_module.close_pool()


async def _rows(db) -> list[dict]:
    return [
        dict(r) for r in await db.fetch(
            "SELECT * FROM audit_log WHERE actor_name = $1 ORDER BY id", ACCOUNT_NAME
        )
    ]


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# -- ordering -----------------------------------------------------------------


def test_the_audit_middleware_is_outermost():
    """`trace_and_audit` wraps `authenticate`, not the other way round.

    Starlette's `add_middleware` inserts at index 0 and the stack is built so
    index 0 is outermost, which means the LAST decorator in the file runs FIRST.
    That is backwards from how the file reads, so it is asserted rather than
    assumed.

    Deliberately not a guard, because reversing the order would not change any
    behaviour tested here: the principal is read after `call_next`, so it is set
    either way. The order matters only so a request rejected by `authenticate`
    is still inside a minted trace -- which nothing consumes yet. This is a
    tripwire for that assumption changing, not proof of a live property, and
    calling it a guard would overstate it.
    """
    names = [
        m.kwargs["dispatch"].__name__
        for m in app.user_middleware
        if "dispatch" in m.kwargs
    ]
    assert names.index("trace_and_audit") < names.index("authenticate")


# -- the property: a route the middleware never heard of is still audited -----


async def test_a_route_the_middleware_does_not_know_about_is_audited(
    client, token, db
):
    """A route registered in the test file writes an audit row anyway.

    This is the whole change. `datum_sync/api.py` contains no reference to this
    path, no route table and no decorator on the handler -- the row exists
    because the request passed through a layer, which is the only kind of
    coverage that does not decay as routes are added.

    Guard: AUDIT-010.
    """
    r = await client.post(NEW_ROUTE, headers=_auth(token))
    assert r.status_code == 200

    rows = await _rows(db)
    assert len(rows) == 1
    assert rows[0]["verb"] == "rest.post"
    assert rows[0]["target"] == NEW_ROUTE
    assert rows[0]["target_kind"] == "path"
    assert rows[0]["via"] == "rest"
    assert rows[0]["outcome"] == "ok"
    assert rows[0]["actor_kind"] == "account"
    # NOT NULL in the schema; a row that cannot be joined is the thing the
    # table was built to stop existing.
    assert uuid.UUID(str(rows[0]["trace_id"]))


async def test_each_request_gets_its_own_trace(client, token, db):
    """Two requests are two traces, so rows never merge across requests."""
    await client.post(NEW_ROUTE, headers=_auth(token))
    await client.post(NEW_ROUTE, headers=_auth(token))

    rows = await _rows(db)
    assert len(rows) == 2
    assert rows[0]["trace_id"] != rows[1]["trace_id"]


async def test_duration_is_measured_not_invented(client, token, db):
    """`duration_ms` is a real measurement, so it is present and sane."""
    await client.post(NEW_ROUTE, headers=_auth(token))
    rows = await _rows(db)
    assert rows[0]["duration_ms"] is not None
    assert 0 <= rows[0]["duration_ms"] < 60_000


# -- the boundary -------------------------------------------------------------


async def test_a_read_is_not_a_row(client, token, db):
    """GET writes nothing: reads are excluded per 10 §1.

    Guard: AUDIT-011.
    """
    r = await client.get("/rest/v1/whoami", headers=_auth(token))
    assert r.status_code == 200
    assert await _rows(db) == []


async def test_an_unauthenticated_write_is_not_a_row(client, db):
    """A 401 writes no row, because `actor_id` is NOT NULL.

    Asserted rather than left implicit because "every write is audited" is what
    this change looks like from outside, and failed authentication is precisely
    the event someone would go looking for. It is not here. Closing that needs
    the column to be nullable, and is recorded in the backport plan under C1.
    """
    r = await client.post(NEW_ROUTE)
    assert r.status_code == 401
    assert await _rows(db) == []


async def test_a_failed_request_is_recorded_as_error(client, token, db):
    """A 4xx writes outcome='error' and carries the status in `error_code`.

    Guard: AUDIT-012.
    """
    r = await client.post(
        "/rest/v1/jobs/not-a-uuid/cancel", headers=_auth(token)
    )
    assert r.status_code >= 400

    rows = await _rows(db)
    assert len(rows) == 1
    assert rows[0]["outcome"] == "error"
    assert rows[0]["error_code"] == r.status_code


async def test_mcp_writes_its_own_rows_and_gains_no_duplicate(client, token, db):
    """`/mcp` keeps writing its rich rows, and the middleware adds none.

    `011_audit_log.sql` states that `audit_log` and `mcp_call_log` agreeing row
    for row is the check on phase one of the audit spine. A middleware row per
    `/mcp` post would break that agreement -- and would do it silently, by
    retiring a check rather than failing a test.

    Guard: AUDIT-013.
    """
    r = await client.post(
        "/mcp",
        headers=_auth(token),
        json={"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {}},
    )
    assert r.status_code == 200

    rows = await _rows(db)
    assert [row["verb"] for row in rows] == ["mcp.ping"]


async def test_the_mcp_row_uses_the_trace_the_middleware_minted(client, token, db):
    """The endpoint reads the request trace instead of minting a second one.

    Two mints for one request would produce rows that look joined -- each set
    internally consistent -- while belonging to traces that cannot be joined to
    each other. The observable version of that here is that `/mcp` and the
    middleware agree, which they can only do by sharing one value.

    Guard: AUDIT-014.
    """
    seen: list[str] = []
    original = audit.Trace.mint

    def _record(client_id):
        trace = original(client_id)
        seen.append(trace.id)
        return trace

    audit.Trace.mint = staticmethod(_record)
    try:
        await client.post(
            "/mcp",
            headers=_auth(token),
            json={"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {}},
        )
    finally:
        audit.Trace.mint = original

    # One mint for one request, and the row carries it.
    assert len(seen) == 1
    rows = await _rows(db)
    assert str(rows[0]["trace_id"]) == seen[0]


# -- failure policy -----------------------------------------------------------


async def test_a_request_survives_an_unwritable_audit_row(client, token, monkeypatch):
    """A failure inside the audit block answers the request and counts the loss.

    `audit.write` swallows its own failures, but it takes an already-open
    connection -- so the case it structurally cannot see is the acquire before
    it failing, where the caller never reaches `write` at all. Whatever the
    cause, the two properties are the same: the caller still gets its response,
    and the missing row still increments `dropped()`. A gap that reports zero is
    worse than no counter, because `/health` would report an intact audit trail.

    Raising from `write` rather than from `db.pool` is deliberate: the pool is
    also what `authenticate` uses, so breaking it fails the request upstream and
    the test would pass without the branch below ever running -- which is how
    the first version of this test failed, for the wrong reason.

    Guard: AUDIT-015.
    """
    async def _explode(*args, **kwargs):
        raise RuntimeError("audit backend is gone")

    monkeypatch.setattr(audit, "write", _explode)
    before = audit.dropped()

    r = await client.post(NEW_ROUTE, headers=_auth(token))

    assert r.status_code == 200
    assert audit.dropped() == before + 1
