"""The gateway as each proposed client drives it (spec/agent-auth-plane/12 §5).

Cases 1, 2 and 5 are sessions and names; 3, 4 and 6 are the OAuth shapes
the two CLIs send (12 §3). The wire is httpx against the app rather than the Python `mcp`
SDK, which is not a dependency of this repository; the request shapes are
the SDK's.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import pytest_asyncio

from datum_sync import auth, db as db_module, mcp, tokens
from datum_sync.api import app

pytestmark = pytest.mark.asyncio

ACCOUNT = "_pytest_conf"
AGENT = "_pytest_conf_agent"
PASSWORD = "conformance-pass-1"
LOOPBACK = "http://localhost:1455/auth/callback"


@pytest_asyncio.fixture
async def conf(db):
    await db.execute("DELETE FROM service_accounts WHERE name IN ($1, $2)", ACCOUNT, AGENT)
    await db.execute("DELETE FROM repositories WHERE name = 'a-very-long-repository-name-for-conformance'")
    account_id = await db.fetchval(
        "INSERT INTO service_accounts (name, max_tier, repo_scope, password_hash) "
        "VALUES ($1, 4, ARRAY['*'], $2) RETURNING id",
        ACCOUNT, auth.hash_password(PASSWORD))
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
    await db.execute("DELETE FROM oauth_clients WHERE client_name LIKE '_pytest_conf%'")
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


# -- OAuth shapes (WP4) -----------------------------------------------------------


def _pkce():
    verifier = secrets.token_urlsafe(64)[:96]
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return verifier, base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


async def _login(c, client_id, scope, on_behalf_of=""):
    """Consent and exchange, as the CLI's callback would."""
    verifier, challenge = _pkce()
    r = await c.post("/oauth/authorize", data={
        "username": ACCOUNT, "password": PASSWORD, "client_id": client_id, "redirect_uri": LOOPBACK,
        "code_challenge": challenge, "resource": auth.MCP_RESOURCE, "state": "st", "scope": scope,
        "on_behalf_of": on_behalf_of})
    assert r.status_code == 302, r.text
    code = parse_qs(urlparse(r.headers["location"]).query)["code"][0]
    r = await c.post("/oauth/token", data={
        "grant_type": "authorization_code", "code": code, "redirect_uri": LOOPBACK,
        "code_verifier": verifier, "client_id": client_id, "resource": auth.MCP_RESOURCE})
    assert r.status_code == 200, r.text
    return r.json()


async def test_case_3_codex_registers_without_scopes_then_asks(conf):
    """Codex registers with no `scope` (openai/codex #20503) and names it at
    authorize; the sponsor binds the grant to the agent on the consent page."""
    c = conf["client"]
    r = await c.post("/oauth/register", json={
        "client_name": "_pytest_conf Codex CLI 0.x", "redirect_uris": [LOOPBACK],
        "grant_types": ["authorization_code", "refresh_token"], "token_endpoint_auth_method": "none"})
    assert r.status_code == 201, r.text
    client_id = r.json()["client_id"]
    _, challenge = _pkce()
    r = await c.get("/oauth/authorize", params={
        "client_id": client_id, "redirect_uri": LOOPBACK, "response_type": "code",
        "code_challenge": challenge, "code_challenge_method": "S256", "scope": "mcp:operate",
        "resource": auth.MCP_RESOURCE, "state": "st"})
    assert r.status_code == 200 and "mcp:operate" in r.text
    body = await _login(c, client_id, "mcp:operate", on_behalf_of=AGENT)
    me = (await c.get("/rest/v1/whoami", headers={"authorization": f"Bearer {body['access_token']}"})).json()
    assert me["name"] == AGENT and me["effective_tier"] == 3, me


async def test_case_4_claude_code_sees_the_picker_and_picks_baseline(conf):
    """Claude Code sends whatever `scopes_supported` says, or nothing when
    `oauth.scopes` is unset; either way the consent page carries the choice."""
    c = conf["client"]
    disc = (await c.get("/.well-known/oauth-authorization-server")).json()
    assert set(disc["scopes_supported"]) >= {"mcp", "mcp:operate"}
    r = await c.post("/oauth/register", json={
        "client_name": "_pytest_conf Claude Code", "redirect_uris": [LOOPBACK]})
    client_id = r.json()["client_id"]
    _, challenge = _pkce()
    r = await c.get("/oauth/authorize", params={
        "client_id": client_id, "redirect_uri": LOOPBACK, "response_type": "code",
        "code_challenge": challenge, "code_challenge_method": "S256"})
    assert r.status_code == 200 and 'name=scope' in r.text and "mcp:operate" in r.text
    body = await _login(c, client_id, "mcp")
    me = (await c.get("/rest/v1/whoami", headers={"authorization": f"Bearer {body['access_token']}"})).json()
    assert me["name"] == ACCOUNT and me["effective_tier"] <= 2, me


async def test_case_6_a_replayed_refresh_revokes_the_family(conf):
    c = conf["client"]
    r = await c.post("/oauth/register", json={
        "client_name": "_pytest_conf Claude Code", "redirect_uris": [LOOPBACK]})
    client_id = r.json()["client_id"]
    first = await _login(c, client_id, "mcp:operate")
    r = await c.post("/oauth/token", data={
        "grant_type": "refresh_token", "refresh_token": first["refresh_token"], "client_id": client_id})
    assert r.status_code == 200, r.text
    second = r.json()
    r = await c.post("/oauth/token", data={
        "grant_type": "refresh_token", "refresh_token": first["refresh_token"], "client_id": client_id})
    assert r.status_code == 400 and r.json()["error"] == "invalid_grant"
    r, sid = await _init(c, second["access_token"], "claude-code")
    assert r.status_code == 401, r.text
