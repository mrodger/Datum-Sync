"""An error response carries the trace id that the audit row was written under.

This is what makes the trace worth minting in a middleware. C1 put a row in
`audit_log` for every write request; without the id in the response body, a
caller reporting "it failed" can offer a timestamp and a path, and someone has
to guess which row that was.

So the load-bearing assertion here is not "the body has a trace_id" and not
"the trace_id is a uuid" -- both pass against a value invented in `envelope()`
that joins to nothing. It is that the id in the body is *the same id* as in this
request's `audit_log` row.

That matters because `envelope()` reads the trace with `getattr(..., None)`
rather than `request.state.trace`. The leniency is deliberate -- an error path is
the worst place to add a second way to fail -- but its cost is that a missing
trace degrades to `"trace_id": null` while every test that checks the envelope's
*shape* still passes. These tests are what turns that into a failure.

Writing these found a gap in C1 rather than confirming it: an unhandled
exception produced a plain-text 500 with no envelope **and no audit row at
all**, because ServerErrorMiddleware is installed outside every user middleware,
so `call_next` raises instead of returning. Both halves are covered below.
"""
from __future__ import annotations

import uuid

import asyncpg
import httpx
import pytest
import pytest_asyncio

from datum_sync import db as db_module, tokens
from datum_sync.api import app
from datum_sync.errors import ApiError

pytestmark = pytest.mark.asyncio

ACCOUNT_NAME = "_err_trace_acct"

# Two failing write routes, registered here and never mentioned in
# `datum_sync/errors.py`. Same reasoning as the audit middleware probe: the
# handlers have no route list, so a route they have never seen is the honest
# test. They are also the only way to reach these two handler paths without
# depending on some other feature's error behaviour staying the same.
#
# BOOM raises a bare ValueError, so it escapes every registered handler and
# takes the ServerErrorMiddleware path -- the case a caller is most likely to be
# reporting, and the one that was returning 21 bytes of text/plain.
BOOM_ROUTE = "/rest/v1/_err_trace_boom"
CONFLICT_ROUTE = "/rest/v1/_err_trace_conflict"


@app.post(BOOM_ROUTE, include_in_schema=False)
async def _err_trace_boom() -> dict:
    raise ValueError("deliberate: an exception no handler claims")


@app.post(CONFLICT_ROUTE, include_in_schema=False)
async def _err_trace_conflict() -> dict:
    raise ApiError(409, "CONFLICT", "deliberate")


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
    """`raise_app_exceptions=False` so a crash is answered, not re-raised here.

    The default re-raises the application's exception into the test, which is
    the one behaviour that makes the 500 response impossible to look at -- and
    the response is the entire subject of this file.
    """
    await db_module.init_pool()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://testserver",
    ) as c:
        yield c
    await db_module.close_pool()


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _row(db) -> dict | None:
    r = await db.fetchrow(
        "SELECT * FROM audit_log WHERE actor_name = $1 ORDER BY id", ACCOUNT_NAME
    )
    return dict(r) if r else None


async def test_a_handled_error_body_joins_to_its_audit_row(db, client, token):
    """The whole point: quote the trace id from the body, find the row.

    Guard: ERROR-001.
    """
    r = await client.post(CONFLICT_ROUTE, headers=_auth(token))
    assert r.status_code == 409
    body_trace = r.json()["trace_id"]
    assert body_trace is not None

    row = await _row(db)
    assert row is not None, "the request was not audited at all"
    assert str(row["trace_id"]) == body_trace
    assert row["outcome"] == "error"
    assert row["error_code"] == 409


async def test_a_crash_is_an_envelope_and_not_plain_text(db, client, token):
    """An unhandled exception used to answer with 21 bytes of text/plain.

    No `code` to branch on, no trace to quote -- on the one failure a caller is
    most likely to be reporting. Reaching the envelope at all requires a handler
    registered for `Exception`, because that is what ServerErrorMiddleware uses,
    and it sits outside every user middleware.

    Guard: ERROR-002.
    """
    r = await client.post(BOOM_ROUTE, headers=_auth(token))
    assert r.status_code == 500
    assert r.headers["content-type"].startswith("application/json")
    body = r.json()
    assert set(body) == {"status", "code", "message", "detail", "trace_id"}
    assert body["code"] == "INTERNAL_ERROR"
    assert body["trace_id"] is not None


async def test_a_crash_still_writes_an_audit_row_and_it_joins(db, client, token):
    """The other half: a crash arrives at the middleware as a raise.

    Before the `except` branch in `trace_and_audit`, this request produced no
    row at all -- measured, not assumed. That made a crash the single request
    type with nothing recorded, which is the inverse of what an audit trail is
    for.

    Guard: ERROR-003.
    """
    r = await client.post(BOOM_ROUTE, headers=_auth(token))
    assert r.status_code == 500

    row = await _row(db)
    assert row is not None, "a crash left no audit row"
    assert row["target"] == BOOM_ROUTE
    assert row["outcome"] == "error"
    assert row["error_code"] == 500
    assert str(row["trace_id"]) == r.json()["trace_id"]


async def test_a_crash_does_not_leak_the_exception_text(client, token):
    """`str(exc)` is written for a developer, and can carry a path or a DSN.

    Guard: ERROR-004.
    """
    r = await client.post(BOOM_ROUTE, headers=_auth(token))
    assert "deliberate" not in r.text


async def test_two_failures_do_not_share_a_trace(client, token):
    """A constant trace id is worse than none -- it joins everything.

    Guard: ERROR-005.
    """
    seen = set()
    for _ in range(3):
        r = await client.post(CONFLICT_ROUTE, headers=_auth(token))
        seen.add(r.json()["trace_id"])
    assert len(seen) == 3


async def test_the_challenge_header_survives_alongside_the_trace(client):
    """The 401 envelope gained an argument; the header it carries is why it exists.

    `envelope()` takes `request` as a new *first* positional parameter, so every
    call site shifted by one. A shift that silently dropped `headers` off the end
    would strip the RFC 9728 challenge an MCP client needs to find the
    authorization server, and the status code would still be 401.

    No guard of its own: AUTH-008 already breaks exactly this call site, and a
    second case on the same two lines would prove nothing the first does not.
    """
    r = await client.post(CONFLICT_ROUTE)
    assert r.status_code == 401
    assert "WWW-Authenticate" in r.headers
    assert r.json()["trace_id"] is not None


async def test_a_rejected_request_carries_a_trace_it_cannot_look_up(client):
    """An unauthenticated 401 gets an id, and it deliberately joins to nothing.

    The 401 is built in the `authenticate` middleware, which sits inside
    `trace_and_audit`, so the trace exists by the time the envelope is made.
    But `audit_log.actor_id` is NOT NULL and a rejected request has no actor, so
    there is no row to find.

    Asserted rather than left implicit, because "every error id joins to a row"
    is what this feature looks like from outside, and for the one class of
    failure someone is most likely to be chasing -- a credential that does not
    work -- it is false. Closing it needs the column nullable; tracked, not
    fixed here. No guard: this pins current behaviour rather than defending a
    decision, and it should be deleted when failed auth becomes auditable.
    """
    r = await client.post(CONFLICT_ROUTE)
    assert r.status_code == 401
    uuid.UUID(r.json()["trace_id"])  # raises if it is not a real id
