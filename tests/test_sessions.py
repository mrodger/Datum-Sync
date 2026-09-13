"""MCP sessions: counted, superseded on the same credential, ended by revocation.

spec/agent-auth-plane/03 §5 and 12 §2. The wire is driven with httpx against
the app; `_init` is what a client's `initialize` looks like.
"""
from __future__ import annotations

import asyncio
import json

import asyncpg
import httpx
import pytest
import pytest_asyncio

from datum_sync import config, db as db_module, mcp, tokens
from datum_sync.api import app

pytestmark = pytest.mark.asyncio

ACCOUNT = "_pytest_sess"
AGENT = "_pytest_sess_agent"


@pytest_asyncio.fixture
async def sess(db):
    """A tier-4 human with two capped tokens, and an agent child with one."""
    await db.execute("DELETE FROM service_accounts WHERE name IN ($1, $2)", ACCOUNT, AGENT)
    account_id = await db.fetchval(
        "INSERT INTO service_accounts (name, max_tier, repo_scope, limits) "
        "VALUES ($1, 4, ARRAY['*'], '{}') RETURNING id", ACCOUNT)
    _, a1 = await tokens.create(db, account_id, "a1", max_tier=2)
    _, a2 = await tokens.create(db, account_id, "a2", max_tier=2)
    _, full = await tokens.create(db, account_id, "full")
    agent_id = await db.fetchval(
        "INSERT INTO service_accounts (name, kind, parent_id, max_tier, repo_scope) "
        "VALUES ($1, 'agent', $2, 2, ARRAY['*']) RETURNING id", AGENT, account_id)
    _, agent = await tokens.create(db, agent_id, "baseline", max_tier=2)
    await db_module.init_pool()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield {"client": c, "a1": a1, "a2": a2, "full": full, "agent": agent,
               "account_id": account_id, "agent_id": agent_id, "db": db}
    await db_module.close_pool()
    await db.execute("DELETE FROM service_accounts WHERE name IN ($1, $2)", ACCOUNT, AGENT)


def _rpc(method, params=None, id_=1):
    return {"jsonrpc": "2.0", "id": id_, "method": method, "params": params or {}}


async def _init(client, token, name="test-client"):
    return await client.post("/mcp", json=_rpc("initialize", {
        "protocolVersion": mcp.PROTOCOL_VERSION, "clientInfo": {"name": name, "version": "1"},
        "capabilities": {}}), headers={"authorization": f"Bearer {token}"})


def _h(token, sid=None):
    h = {"authorization": f"Bearer {token}"}
    if sid:
        h["Mcp-Session-Id"] = sid
    return h


async def test_a_second_credential_is_refused_at_the_limit(sess):
    """SESS-001: two capped tokens on one principal, limit 1 (baseline).

    Guard: SESS-001.
    """
    c = sess["client"]
    r = await _init(c, sess["a1"])
    assert r.status_code == 200 and r.headers.get("Mcp-Session-Id"), r.text
    r = await _init(c, sess["a2"])
    assert r.status_code == 200
    err = r.json()["error"]
    assert err["code"] == mcp.SESSION_LIMIT
    assert err["data"]["limit"] == 1 and len(err["data"]["active"]) == 1
    assert err["data"]["active"][0]["client_name"] == "test-client"


async def test_the_same_credential_supersedes_its_own_session(sess):
    """SESS-005: a reconnecting client is the same conversation (12 §2).

    Guard: SESS-005.
    """
    c = sess["client"]
    first = (await _init(c, sess["a1"])).headers["Mcp-Session-Id"]
    r = await _init(c, sess["a1"])
    assert r.status_code == 200 and "error" not in r.json()
    second = r.headers["Mcp-Session-Id"]
    assert second != first and r.headers.get("X-Datum-Superseded-Session") == first
    row = await sess["db"].fetchrow("SELECT ended_at, end_reason FROM mcp_sessions WHERE id = $1", first)
    assert row["ended_at"] is not None and row["end_reason"] == "superseded"
    # The old id is dead; the new one works.
    r = await c.post("/mcp", json=_rpc("ping"), headers=_h(sess["a1"], first))
    assert r.status_code == 404
    r = await c.post("/mcp", json=_rpc("ping"), headers=_h(sess["a1"], second))
    assert r.status_code == 200 and r.json()["result"] == {}
    # And a different credential is still refused: supersession did not raise the limit.
    r = await _init(c, sess["a2"])
    assert r.json()["error"]["code"] == mcp.SESSION_LIMIT


async def test_a_session_is_bound_to_its_principal(sess):
    """SESS-002: another principal's session id is a 404 with no hint.

    Guard: SESS-002.
    """
    c = sess["client"]
    sid = (await _init(c, sess["full"])).headers["Mcp-Session-Id"]
    r = await c.post("/mcp", json=_rpc("ping"), headers=_h(sess["agent"], sid))
    assert r.status_code == 404 and not r.content


async def test_revoking_the_token_ends_its_session(sess):
    """SESS-003.

    Guard: SESS-003.
    """
    c = sess["client"]
    sid = (await _init(c, sess["a1"])).headers["Mcp-Session-Id"]
    assert await tokens.revoke(sess["db"], sess["account_id"], "a1")
    row = await sess["db"].fetchrow("SELECT ended_at, end_reason FROM mcp_sessions WHERE id = $1", sid)
    assert row["ended_at"] is not None and row["end_reason"] == "revoked"
    r = await c.post("/mcp", json=_rpc("ping"), headers=_h(sess["a1"], sid))
    assert r.status_code == 401


async def test_agents_cannot_skip_initialize(sess):
    """SESS-004: humans get one release of grace; agents do not.

    Guard: SESS-004.
    """
    c = sess["client"]
    r = await c.post("/mcp", json=_rpc("tools/list"), headers=_h(sess["agent"]))
    assert r.status_code == 400 and r.json()["code"] == "SESSION_REQUIRED"
    r = await c.post("/mcp", json=_rpc("tools/list"), headers=_h(sess["full"]))
    assert r.status_code == 200 and "result" in r.json()
    sid = (await _init(c, sess["agent"])).headers["Mcp-Session-Id"]
    r = await c.post("/mcp", json=_rpc("tools/list"), headers=_h(sess["agent"], sid))
    assert r.status_code == 200


async def test_the_idle_window_is_per_principal(sess):
    """SESS-006: `limits.session_idle_seconds` makes a stale session not count.

    Guard: SESS-006.
    """
    c, db = sess["client"], sess["db"]
    await db.execute("UPDATE service_accounts SET limits = '{\"session_idle_seconds\": 1}' WHERE id = $1",
                     sess["account_id"])
    sid = (await _init(c, sess["a1"])).headers["Mcp-Session-Id"]
    r = await _init(c, sess["a2"])
    assert r.json()["error"]["code"] == mcp.SESSION_LIMIT
    await asyncio.sleep(1.3)
    r = await _init(c, sess["a2"])
    assert "error" not in r.json(), "the idle session no longer counts"
    r = await c.post("/mcp", json=_rpc("ping"), headers=_h(sess["a1"], sid))
    assert r.status_code == 404, "and it cannot be resumed"


async def test_delete_ends_the_session_and_session_info_describes_it(sess):
    c = sess["client"]
    sid = (await _init(c, sess["a1"])).headers["Mcp-Session-Id"]
    r = await c.post("/mcp", json=_rpc("tools/call", {"name": "session_info", "arguments": {}}),
                     headers=_h(sess["a1"], sid))
    body = r.json()["result"]["structuredContent"]
    assert body["session_id"] == sid and body["limit"] == 1 and body["live"][0]["this"] is True
    r = await c.request("DELETE", "/mcp", headers=_h(sess["a1"], sid))
    assert r.status_code == 204
    r = await c.post("/mcp", json=_rpc("ping"), headers=_h(sess["a1"], sid))
    assert r.status_code == 404
    listed = (await c.get(f"/rest/v1/principals/{ACCOUNT}/sessions", headers=_h(sess["full"]))).json()
    assert listed["limit"] == 4 and any(s["id"] == sid and s["end_reason"] == "client" for s in listed["items"])


async def test_audit_rows_carry_the_session(sess):
    c = sess["client"]
    sid = (await _init(c, sess["a1"])).headers["Mcp-Session-Id"]
    await c.post("/mcp", json=_rpc("tools/list"), headers=_h(sess["a1"], sid))
    n = await sess["db"].fetchval(
        "SELECT count(*) FROM audit_log WHERE session_id = $1 AND verb = 'mcp.tools.list'", sid)
    assert n == 1


async def test_whoami_tool_reports_the_ceiling_and_the_way_up(sess):
    c = sess["client"]
    sid = (await _init(c, sess["a1"])).headers["Mcp-Session-Id"]
    r = await c.post("/mcp", json=_rpc("tools/call", {"name": "whoami", "arguments": {}}),
                     headers=_h(sess["a1"], sid))
    body = r.json()["result"]["structuredContent"]
    assert body["effective_tier"] == 2 and body["session"]["id"] == sid
    assert "mcp:operate" in body["elevate"]


# -- tool names ------------------------------------------------------------------


def test_tool_names_are_capped_stably():
    """MCP-020 (spec 12 §1).

    Guard: MCP-020.
    """
    short = mcp.tool_name("SCIMAC", "site_plan")
    assert short == "SCIMAC__site_plan"
    long_a = mcp.tool_name("a-very-long-repository-name-for-testing", "and-a-very-long-workspace-name-too")
    long_b = mcp.tool_name("a-very-long-repository-name-for-testing", "and-a-very-long-workspace-name-two")
    assert len(long_a) <= mcp.MAX_TOOL_NAME and len(long_b) <= mcp.MAX_TOOL_NAME
    assert long_a != long_b
    assert long_a == mcp.tool_name("a-very-long-repository-name-for-testing", "and-a-very-long-workspace-name-too")
    import re
    assert re.fullmatch(r"[A-Za-z0-9_.-]+", long_a)


# -- job limits ------------------------------------------------------------------


async def test_job_limits_are_counted_in_the_database(sess, workspace):
    """TIER-010: jobs_per_hour and concurrent_jobs, per principal.

    Guard: TIER-010.
    """
    c, db = sess["client"], sess["db"]
    repo, ws = workspace
    await db.execute(
        "UPDATE service_accounts SET limits = '{\"jobs_per_hour\": 2, \"concurrent_jobs\": 5}' WHERE id = $1",
        sess["account_id"])
    h = _h(sess["full"])
    for _ in range(2):
        r = await c.post(f"/rest/v1/transformations/submit/{repo}/{ws}", json={"params": {"WHO": "x"}}, headers=h)
        assert r.status_code == 202, r.text
    r = await c.post(f"/rest/v1/transformations/submit/{repo}/{ws}", json={"params": {"WHO": "x"}}, headers=h)
    assert r.status_code == 429 and r.json()["code"] == "JOB_LIMIT"
    assert r.json()["detail"]["limit"] == "jobs_per_hour"
    await db.execute(
        "UPDATE service_accounts SET limits = '{\"jobs_per_hour\": 50, \"concurrent_jobs\": 2}' WHERE id = $1",
        sess["account_id"])
    r = await c.post(f"/rest/v1/transformations/submit/{repo}/{ws}", json={"params": {"WHO": "x"}}, headers=h)
    assert r.status_code == 429 and r.json()["detail"]["limit"] == "concurrent_jobs"
    row = await db.fetchrow("SELECT grant_snapshot FROM jobs WHERE submitted_by = $1 LIMIT 1", ACCOUNT)
    snap = json.loads(row["grant_snapshot"])
    assert snap["effective_tier"] == 4 and snap["principal_name"] == ACCOUNT and snap["kind"] == "human"
    await db.execute("DELETE FROM jobs WHERE submitted_by = $1", ACCOUNT)
