"""The gateway as each proposed client drives it (spec/agent-auth-plane/12 §5).

Cases 1, 2 and 5 here (sessions and names); 3, 4 and 6 (OAuth shapes) arrive
with WP4. The wire is httpx against the app rather than the Python `mcp`
SDK, which is not a dependency of this repository; the request shapes are
the SDK's.
"""
from __future__ import annotations

import json
import re

import httpx
import pytest
import pytest_asyncio

from datum_sync import db as db_module, mcp, tokens
from datum_sync.api import app

pytestmark = pytest.mark.asyncio

ACCOUNT = "_pytest_conf"
AGENT = "_pytest_conf_agent"


@pytest_asyncio.fixture
async def conf(db):
    await db.execute("DELETE FROM service_accounts WHERE name IN ($1, $2)", ACCOUNT, AGENT)
    await db.execute("DELETE FROM repositories WHERE name = 'a-very-long-repository-name-for-conformance'")
    account_id = await db.fetchval(
        "INSERT INTO service_accounts (name, max_tier, repo_scope) VALUES ($1, 4, ARRAY['*']) RETURNING id",
        ACCOUNT)
    agent_id = await db.fetchval(
        "INSERT INTO service_accounts (name, kind, parent_id, max_tier, repo_scope) "
        "VALUES ($1, 'agent', $2, 3, ARRAY['*']) RETURNING id", AGENT, account_id)
    _, t1 = await tokens.create(db, agent_id, "baseline", max_tier=2)
    _, t2 = await tokens.create(db, agent_id, "second", max_tier=2)
    _, full = await tokens.create(db, agent_id, "full")
    repo_id = await db.fetchval(
        "INSERT INTO repositories (name, path) VALUES ('a-very-long-repository-name-for-conformance', '/nonexistent') RETURNING id")
    await db.execute(
        "INSERT INTO workspaces (repository_id, name, version, manifest) VALUES ($1, $2, '1', $3)",
        repo_id, "and-an-equally-long-workspace-name-for-the-cap",
        json.dumps({"name": "x", "version": "1", "parameters": [],
                    "outputs": [{"name": "out", "type": "text/plain", "primary": True}],
                    "services": ["data_streaming"], "timeout_seconds": 5}))
    await db_module.init_pool()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as c:
        yield {"client": c, "t1": t1, "t2": t2, "full": full, "db": db}
    await db_module.close_pool()
    await db.execute("DELETE FROM repositories WHERE name = 'a-very-long-repository-name-for-conformance'")
    await db.execute("DELETE FROM service_accounts WHERE name IN ($1, $2)", ACCOUNT, AGENT)


def _rpc(method, params=None, id_=1):
    return {"jsonrpc": "2.0", "id": id_, "method": method, "params": params or {}}


async def _init(client, token, name):
    r = await client.post("/mcp", json=_rpc("initialize", {
        "protocolVersion": mcp.PROTOCOL_VERSION, "clientInfo": {"name": name, "version": "0"},
        "capabilities": {}}), headers={"authorization": f"Bearer {token}"})
    return r, r.headers.get("Mcp-Session-Id")


def _h(token, sid):
    return {"authorization": f"Bearer {token}", "Mcp-Session-Id": sid}


async def test_case_1_claude_code_reconnects_without_closing(conf):
    """initialize, list, call, then a second initialize on the same token: admitted,
    the first session superseded."""
    c = conf["client"]
    r, sid = await _init(c, conf["t1"], "claude-code")
    assert "error" not in r.json() and sid
    r = await c.post("/mcp", json=_rpc("tools/list"), headers=_h(conf["t1"], sid))
    names = {t["name"] for t in r.json()["result"]["tools"]}
    assert "whoami" in names
    r = await c.post("/mcp", json=_rpc("tools/call", {"name": "whoami", "arguments": {}}),
                     headers=_h(conf["t1"], sid))
    assert r.json()["result"]["isError"] is False
    r2, sid2 = await _init(c, conf["t1"], "claude-code")
    assert "error" not in r2.json() and sid2 != sid
    assert r2.headers["X-Datum-Superseded-Session"] == sid
    row = await conf["db"].fetchrow("SELECT end_reason FROM mcp_sessions WHERE id = $1", sid)
    assert row["end_reason"] == "superseded"


async def test_case_2_datum3_opens_a_session_per_call(conf):
    """Five initialize / call / DELETE cycles on one token never hit the limit;
    a sixth initialize on a second token does."""
    c = conf["client"]
    for i in range(5):
        r, sid = await _init(c, conf["t1"], "datum-3.0")
        assert "error" not in r.json(), f"cycle {i}: {r.text}"
        r = await c.post("/mcp", json=_rpc("tools/call", {"name": "session_info", "arguments": {}}),
                         headers=_h(conf["t1"], sid))
        assert r.json()["result"]["isError"] is False
        r = await c.request("DELETE", "/mcp", headers=_h(conf["t1"], sid))
        assert r.status_code == 204
    r, sid = await _init(c, conf["t1"], "datum-3.0")
    assert "error" not in r.json()
    r2, _ = await _init(c, conf["t2"], "datum-3.0")
    assert r2.json()["error"]["code"] == mcp.SESSION_LIMIT


async def test_case_5_every_tool_name_fits_a_client_prefix(conf):
    c = conf["client"]
    r, sid = await _init(c, conf["full"], "codex")
    r = await c.post("/mcp", json=_rpc("tools/list"), headers=_h(conf["full"], sid))
    names = [t["name"] for t in r.json()["result"]["tools"]]
    assert any(n.startswith("a-very-long-repository") for n in names)
    for n in names:
        assert len(n) <= 48, n
        assert re.fullmatch(r"[A-Za-z0-9_.-]+", n), n
