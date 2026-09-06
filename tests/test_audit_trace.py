"""The audit spine: one server-minted trace id joins the rows of one request.

The thing being proven is narrow and specific. Before this change, an MCP
`proxy_request` tool call wrote one `mcp_call_log` row and one `proxy_log` row
with nothing in common but a timestamp, so "which upstream did this tool call
touch" had no answer. `mcp_call_log.client_trace_id` looked like the answer and
was not: it is the `X-Trace-Id` request header, so the caller chooses it.

So there are two halves here, and the second is the one that matters:

  1. One tool call that reaches the proxy writes two audit rows under ONE trace.
  2. A caller cannot change that grouping. Sending the same `X-Trace-Id` on two
     requests must not merge them, and sending another request's server trace
     as `X-Trace-Id` must not splice into it.

A test of half 1 alone passes just as well against the old design of trusting
the header -- the rows would still share a value. Half 2 is what distinguishes
a minted id from an accepted one.
"""
from __future__ import annotations

import json

import httpx
import pytest
import pytest_asyncio

from datum_sync import (
    audit, auth, connections as conn_mod, crypto, db as db_module, mcp, proxy,
    tokens,
)
from datum_sync.api import app

pytestmark = pytest.mark.asyncio

CONN_NAME = "_audit_trace_conn"
ACCOUNT_NAME = "_audit_trace_acct"
AGENT_NAME = "_audit_trace_agent"
SECRET_KEY = "sk-audit-trace-secret-98765"


# -- unit ---------------------------------------------------------------------


def test_verb_covers_every_dispatchable_method():
    """Every method in METHODS maps to a verb, with the dots the spec uses.

    Iterates METHODS rather than listing the four names, so a method added to
    the dispatch table without an audit verb fails here instead of writing a
    NULL into a NOT NULL column -- which `audit.write` would swallow, dropping
    the row and the trace with it.

    Guard: AUDIT-001.
    """
    verbs = {m: mcp._verb(m) for m in mcp.METHODS}
    assert verbs == {
        "initialize": "mcp.initialize",
        "ping": "mcp.ping",
        "tools/list": "mcp.tools.list",
        "tools/call": "mcp.tools.call",
    }
    assert all("/" not in v for v in verbs.values())


@pytest.mark.parametrize(
    "tool_name, expected_kind",
    [
        ("vault_read", "vault_path"),
        ("vault_write", "vault_path"),
        ("vault_list", "vault_path"),
        ("proxy_request", "connection"),
        ("some_repo__some_ws", "tool"),
    ],
)
def test_target_kind_agrees_with_call_target(tool_name, expected_kind):
    """`_target_kind` labels exactly what `_call_target` returns.

    They are two functions branching over the same constants, so they can
    drift. This is what stops that: whenever `_call_target` produces a target,
    `_target_kind` must produce a label, and the pair must be consistent.
    """
    params = {"name": tool_name, "arguments": {"path": "dev/x.md", "connection": "c"}}
    target = mcp._call_target("tools/call", tool_name, params)
    kind = mcp._target_kind("tools/call", tool_name)
    assert kind == expected_kind
    assert (target is None) == (kind is None)


def test_target_kind_is_none_where_there_is_no_target():
    for method in ("tools/list", "initialize", "ping"):
        assert mcp._target_kind(method, None) is None
        assert mcp._call_target(method, None, {}) is None


def test_minting_gives_a_different_id_every_time():
    """Two mints from the same client string are two different traces.

    Guard: AUDIT-002.
    """
    a = audit.Trace.mint("same-client-value")
    b = audit.Trace.mint("same-client-value")
    assert a.id != b.id
    assert a.client_id == b.client_id == "same-client-value"


def test_detail_is_bounded_and_stays_valid_json():
    """An oversized detail is replaced, not sliced.

    Half a JSON document is not JSON, and jsonb would reject it -- taking the
    whole row down, so an oversized detail would cost us the trace as well.
    """
    small = audit._bounded({"a": 1})
    assert json.loads(small) == {"a": 1}

    big = audit._bounded({"blob": "x" * (audit.MAX_DETAIL_BYTES + 1), "other": 2})
    parsed = json.loads(big)          # must still parse
    assert parsed["truncated"] is True
    assert parsed["keys"] == ["blob", "other"]
    assert "x" * 100 not in big

    assert audit._bounded(None) is None


# -- fixtures -----------------------------------------------------------------


@pytest_asyncio.fixture
async def db():
    import asyncpg
    from datum_sync import config

    try:
        conn = await asyncpg.connect(config.DATABASE_URL, timeout=3)
    except Exception:
        pytest.skip("database unavailable")
    yield conn
    await conn.execute("DELETE FROM audit_log WHERE actor_name = $1", AGENT_NAME)
    await conn.execute("DELETE FROM audit_log WHERE actor_name = $1", ACCOUNT_NAME)
    await conn.execute("DELETE FROM mcp_call_log WHERE account_name = $1", ACCOUNT_NAME)
    await conn.execute("DELETE FROM proxy_log WHERE agent_name = $1", AGENT_NAME)
    await conn.execute("DELETE FROM agents WHERE name = $1", AGENT_NAME)
    await conn.execute("DELETE FROM connections WHERE name = $1", CONN_NAME)
    await conn.execute("DELETE FROM service_accounts WHERE name = $1", ACCOUNT_NAME)
    await conn.close()


@pytest_asyncio.fixture
async def setup(db, monkeypatch):
    if not crypto.available():
        crypto.set_test_keys(monkeypatch)

    account_id = await db.fetchval(
        """
        INSERT INTO service_accounts (name, max_tier, is_admin)
        VALUES ($1, 4, true) RETURNING id
        """,
        ACCOUNT_NAME,
    )
    _, account_token = await tokens.create(db, account_id, "fixture")

    agent_token = auth.new_token()
    await db.execute(
        """
        INSERT INTO agents (account_id, name, token_hash, proxy_grants)
        VALUES ($1, $2, $3, $4)
        """,
        account_id, AGENT_NAME, auth.hash_token(agent_token), [CONN_NAME],
    )

    await conn_mod.create(
        db,
        name=CONN_NAME,
        type_="http",
        config={
            "base_url": "https://api.example.com",
            "auth_inject": {"type": "bearer", "secret_field": "api_key"},
        },
        secret={"api_key": SECRET_KEY},
        tier=2,
    )
    return agent_token, account_token


class _FakeUpstream:
    """Stands in for httpx.AsyncClient inside datum_sync.proxy only."""

    def __init__(self, status: int = 200):
        self.status = status

    def __call__(self, *args, **kwargs):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def request(self, method, url, **kwargs):
        return httpx.Response(
            self.status,
            content=b'{"ok": true}',
            headers={"content-type": "application/json"},
            request=httpx.Request(method, url),
        )


@pytest_asyncio.fixture
async def client(db, setup, monkeypatch):
    fake = _FakeUpstream()
    monkeypatch.setattr(proxy, "httpx", type("_httpx", (), {"AsyncClient": fake}))
    # api.example.com does not resolve; the SSRF guard has its own tests.
    monkeypatch.setattr(proxy, "validate_upstream_url", lambda url: url)

    await db_module.init_pool()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as c:
        yield c
    await db_module.close_pool()


def _proxy_call(path: str = "/v1/test"):
    return {
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {
            "name": "proxy_request",
            "arguments": {"connection": CONN_NAME, "method": "GET", "path": path},
        },
    }


async def _rows(db, actor: str = AGENT_NAME) -> list[dict]:
    return [
        dict(r) for r in await db.fetch(
            "SELECT * FROM audit_log WHERE actor_name = $1 ORDER BY id", actor
        )
    ]


# -- half 1: the rows of one request join -------------------------------------


async def test_one_trace_covers_the_mcp_row_and_the_proxy_row(client, setup, db):
    """One tools/call reaching the proxy writes two rows under one trace.

    This is the question that had no answer before: which upstream did this
    tool call touch. The assertion is on the count as well as the identity --
    "the trace ids I found are all equal" is vacuously true of one row, and one
    row is exactly what a half-wired dual-write produces.

    Guard: AUDIT-003.
    """
    agent_token, _ = setup
    r = await client.post(
        "/mcp", json=_proxy_call(),
        headers={"authorization": f"Bearer {agent_token}"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["result"]["isError"] is False

    rows = await _rows(db)
    assert len(rows) == 2, [row["verb"] for row in rows]
    assert {row["verb"] for row in rows} == {"mcp.tools.call", "proxy.request"}
    assert len({row["trace_id"] for row in rows}) == 1

    proxy_row = next(r for r in rows if r["verb"] == "proxy.request")
    assert proxy_row["target"] == f"{CONN_NAME}:GET:/v1/test"
    assert proxy_row["outcome"] == "ok"
    assert json.loads(proxy_row["detail"])["upstream_status"] == 200

    mcp_row = next(r for r in rows if r["verb"] == "mcp.tools.call")
    assert mcp_row["target_kind"] == "connection"
    # The agent's account is not a column, so it has to be recoverable here.
    assert json.loads(mcp_row["detail"])["account"] == ACCOUNT_NAME


async def test_a_call_that_never_reaches_the_proxy_writes_one_row(client, setup, db):
    """tools/list writes only its own row -- the pair is not unconditional.

    Without this, `test_one_trace_covers...` would still pass against a writer
    that emitted a proxy row for every request regardless of what happened.
    """
    agent_token, _ = setup
    await client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        headers={"authorization": f"Bearer {agent_token}"},
    )
    rows = await _rows(db)
    assert [r["verb"] for r in rows] == ["mcp.tools.list"]
    assert rows[0]["target"] is None and rows[0]["target_kind"] is None


async def test_an_upstream_failure_is_recorded_as_an_error(client, setup, db, monkeypatch):
    """A 500 from upstream is outcome='error' with the status as error_code."""
    agent_token, _ = setup
    monkeypatch.setattr(
        proxy, "httpx", type("_httpx", (), {"AsyncClient": _FakeUpstream(500)})
    )
    await client.post(
        "/mcp", json=_proxy_call(),
        headers={"authorization": f"Bearer {agent_token}"},
    )
    proxy_row = next(r for r in await _rows(db) if r["verb"] == "proxy.request")
    assert proxy_row["outcome"] == "error"
    assert proxy_row["error_code"] == 500


# -- half 2: the caller cannot change the grouping ----------------------------


async def test_a_repeated_client_trace_does_not_merge_two_requests(client, setup, db):
    """The same X-Trace-Id on two requests still yields two distinct traces.

    This is the assertion the old `client_trace_id` column cannot satisfy. An
    agent sending one fixed header value on every call would, under a design
    that trusted the header, collapse its entire history into a single trace --
    and an audit trail in which everything is one event is not an audit trail.

    Guard: AUDIT-004.
    """
    agent_token, _ = setup
    headers = {
        "authorization": f"Bearer {agent_token}",
        "X-Trace-Id": "i-am-always-this-value",
    }
    await client.post("/mcp", json=_proxy_call("/one"), headers=headers)
    await client.post("/mcp", json=_proxy_call("/two"), headers=headers)

    rows = await _rows(db)
    assert len(rows) == 4
    traces = {r["trace_id"] for r in rows}
    assert len(traces) == 2, "two requests must not share one trace"
    # The client's value is kept -- it is useful, just never authoritative.
    assert {r["client_trace_id"] for r in rows} == {"i-am-always-this-value"}

    # And each trace still groups its own pair correctly.
    for trace in traces:
        pair = [r for r in rows if r["trace_id"] == trace]
        assert {r["verb"] for r in pair} == {"mcp.tools.call", "proxy.request"}
        assert len({r["target"] for r in pair if r["verb"] == "proxy.request"}) == 1


async def test_a_client_cannot_splice_itself_into_another_trace(client, setup, db):
    """Sending a real server trace as X-Trace-Id does not join that trace.

    The forgery attempt: read (or guess) another request's trace id and present
    it as your own header, so your rows appear inside their trace. It fails
    because the header is never written to `trace_id` -- it lands in
    `client_trace_id`, where it is visibly a claim rather than a fact.

    Guard: AUDIT-005.
    """
    agent_token, _ = setup
    await client.post(
        "/mcp", json=_proxy_call("/victim"),
        headers={"authorization": f"Bearer {agent_token}"},
    )
    victim_trace = str((await _rows(db))[0]["trace_id"])

    await client.post(
        "/mcp", json=_proxy_call("/attacker"),
        headers={
            "authorization": f"Bearer {agent_token}",
            "X-Trace-Id": victim_trace,
        },
    )

    rows = await _rows(db)
    in_victim_trace = [r for r in rows if str(r["trace_id"]) == victim_trace]
    assert len(in_victim_trace) == 2, "the forged header pulled rows into the trace"
    assert {r["target"] for r in in_victim_trace if r["verb"] == "proxy.request"} == {
        f"{CONN_NAME}:GET:/victim"
    }
    # The attempt is not erased -- it is recorded as what it is.
    forged = [r for r in rows if r["client_trace_id"] == victim_trace]
    assert forged and all(str(r["trace_id"]) != victim_trace for r in forged)


# -- dual-write: the new table must agree with the old ------------------------


async def test_the_audit_row_agrees_with_the_mcp_call_log_row(client, setup, db):
    """Phase one mirrors the existing writer, so the two tables must match.

    That agreement is the check on the new table before anything is retired.
    It is only a valid check because this change deliberately reproduced the
    old classification instead of improving it -- see `_log_call`.

    Guard: AUDIT-006.
    """
    agent_token, _ = setup
    await client.post(
        "/mcp", json=_proxy_call(),
        headers={"authorization": f"Bearer {agent_token}"},
    )
    old = dict(await db.fetchrow(
        "SELECT * FROM mcp_call_log WHERE account_name = $1 ORDER BY id DESC LIMIT 1",
        ACCOUNT_NAME,
    ))
    new = next(r for r in await _rows(db) if r["verb"] == "mcp.tools.call")

    assert new["target"] == old["target"]
    assert new["outcome"] == old["outcome"]
    assert new["error_code"] == old["error_code"]
    assert new["duration_ms"] == old["duration_ms"]
    assert new["governance"] == old["is_governance"]
    assert new["client_trace_id"] == old["client_trace_id"]


async def test_no_audit_row_carries_the_injected_secret(client, setup, db):
    """The audit trail must not become the place the credential leaks.

    The proxy injects a secret into the outgoing request. Nothing in these rows
    is derived from headers or bodies, so this holds by construction -- which is
    exactly why it is worth asserting: a later change that put a response body
    into `detail` for debugging would break it silently.

    Registered UNPROVABLE, not as a break case. Holding by construction means
    there is no line whose removal breaks it: the change that would make it
    false is an *addition*, and the harness proves a guard by deleting one.
    A case registered here would delete something, watch this test pass, and
    report the guard as not load-bearing -- which is true and useless.

    Guard: AUDIT-007.
    """
    agent_token, _ = setup
    await client.post(
        "/mcp", json=_proxy_call(),
        headers={"authorization": f"Bearer {agent_token}"},
    )
    for row in await _rows(db):
        blob = json.dumps({k: str(v) for k, v in row.items()})
        assert SECRET_KEY not in blob
        assert agent_token not in blob


class _DeadConn:
    """A connection that cannot execute -- the shape of the real failure.

    A dead or exhausted connection, rather than a mocked exception type, so
    the test exercises the same `except Exception` the live path relies on.
    """

    async def execute(self, *args):
        raise ConnectionError("connection is closed")


async def _drop_one() -> None:
    """Force exactly one audit write to fail. Must not raise."""
    from datum_sync.auth import Principal

    await audit.write(
        _DeadConn(),
        trace=audit.Trace.mint(None),
        principal=Principal(
            account_id=1, name=ACCOUNT_NAME, max_tier=4, repo_scope=["*"],
            is_admin=False, source="token",
            vault_scope=None,
        ),
        via="mcp", verb="mcp.ping", target_kind=None, target=None, outcome="ok",
    )


async def test_a_failed_audit_write_is_counted_not_hidden():
    """Break the insert and the counter moves. Never raises either way.

    Guard: AUDIT-009.
    """
    before = audit.dropped()
    await _drop_one()
    assert audit.dropped() == before + 1


async def test_health_reports_the_audit_drop_count(client):
    """A swallowed audit write has to be visible somewhere.

    `audit.write` swallows its exceptions, as every writer beside it does. That
    is the right call for a logging path and the wrong one for an audit trail,
    so the count is surfaced.

    The drop is forced first, deliberately. Asserting `audit_dropped ==
    dropped()` on a clean process compares zero to zero, and passes just as
    happily against a hardcoded `"audit_dropped": 0` -- a field that looks
    wired and reports nothing. A non-zero count is the only version of this
    that can fail.

    Guard: AUDIT-008.
    """
    assert (await client.get("/health")).json()["audit_dropped"] == audit.dropped()

    await _drop_one()
    expected = audit.dropped()
    assert expected > 0

    r = await client.get("/health")
    assert r.status_code == 200
    assert r.json()["audit_dropped"] == expected
