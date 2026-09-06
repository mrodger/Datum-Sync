"""End-to-end smoke test: MCP → proxy_request → auth injection → audit log.

Creates a real service account, agent, and HTTP connection in Postgres.
Monkeypatches httpx.AsyncClient inside datum_sync.proxy so only the upstream
call is intercepted — the test client's own ASGI transport is untouched.

Verifies:
  1. Agent token authenticates through /mcp
  2. proxy_request routes correctly via tools/call
  3. Auth injection puts the secret into the outgoing headers
  4. The audit log row is written
  5. An account token (not agent) is rejected for proxy
  6. An agent without the grant is rejected
"""
from __future__ import annotations

import json

import httpx
import pytest
import pytest_asyncio

from datum_sync import (
    auth, connections as conn_mod, crypto, db as db_module, proxy, tokens,
)
from datum_sync.api import app

pytestmark = pytest.mark.asyncio

CONN_NAME = "_e2e_proxy_conn"
ACCOUNT_NAME = "_e2e_proxy_acct"
AGENT_NAME = "_e2e_proxy_agent"
SECRET_KEY = "sk-test-secret-12345"


# -- fixtures ----------------------------------------------------------------


@pytest_asyncio.fixture
async def db():
    import asyncpg
    from datum_sync import config

    try:
        conn = await asyncpg.connect(config.DATABASE_URL, timeout=3)
    except (OSError, Exception):
        pytest.skip("database unavailable")
    yield conn
    # cleanup in reverse dependency order
    await conn.execute("DELETE FROM proxy_log WHERE agent_name = $1", AGENT_NAME)
    await conn.execute("DELETE FROM agents WHERE name = $1", AGENT_NAME)
    await conn.execute("DELETE FROM connections WHERE name = $1", CONN_NAME)
    await conn.execute("DELETE FROM service_accounts WHERE name = $1", ACCOUNT_NAME)
    await conn.close()


@pytest_asyncio.fixture
async def setup(db, monkeypatch):
    """Create account, agent, and HTTP connection. Returns (agent_token, account_token)."""
    if not crypto.available():
        crypto.set_test_keys(monkeypatch)

    # Service account
    account_id = await db.fetchval(
        """
        INSERT INTO service_accounts (name, max_tier, is_admin)
        VALUES ($1, 4, true)
        RETURNING id
        """,
        ACCOUNT_NAME,
    )
    _, account_token = await tokens.create(db, account_id, "fixture")

    # Agent with proxy grant
    agent_token = auth.new_token()
    await db.execute(
        """
        INSERT INTO agents (account_id, name, token_hash, proxy_grants)
        VALUES ($1, $2, $3, $4)
        """,
        account_id,
        AGENT_NAME,
        auth.hash_token(agent_token),
        [CONN_NAME],
    )

    # HTTP connection with bearer auth injection
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


@pytest_asyncio.fixture
async def client(db, setup):
    await db_module.init_pool()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
    ) as c:
        yield c
    await db_module.close_pool()


class _FakeUpstreamClient:
    """Drop-in for httpx.AsyncClient used inside proxy.proxy_request.

    Captures outgoing requests and returns a 200 with echoed headers.
    """

    def __init__(self, *args, **kwargs):
        self.captured = {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def request(self, method, url, **kwargs):
        self.captured["method"] = method
        self.captured["url"] = str(url)
        self.captured["headers"] = dict(kwargs.get("headers") or {})
        self.captured["params"] = dict(kwargs.get("params") or {})
        echo = json.dumps({
            "method": method,
            "url": str(url),
            "headers": self.captured["headers"],
        })
        return httpx.Response(
            200,
            content=echo.encode(),
            headers={"content-type": "application/json"},
            request=httpx.Request(method, url),
        )


# Shared instance so tests can inspect captured data after the call.
_upstream = _FakeUpstreamClient()


def _mcp_call(connection: str, method: str = "GET", path: str = "/v1/test"):
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": "proxy_request",
            "arguments": {
                "connection": connection,
                "method": method,
                "path": path,
            },
        },
    }


# -- tests -------------------------------------------------------------------


async def test_full_proxy_via_mcp(client, setup, db, monkeypatch):
    """Agent token → /mcp tools/call proxy_request → injected auth → audit log."""
    agent_token, _ = setup

    # Patch httpx.AsyncClient only inside the proxy module
    fake = _FakeUpstreamClient()

    monkeypatch.setattr(proxy, "httpx", type("_httpx", (), {
        "AsyncClient": lambda *a, **kw: fake,
    }))

    # Bypass DNS resolution (api.example.com won't resolve in CI).
    # The SSRF guard is tested thoroughly in test_proxy.py — here we
    # just need it to pass through.
    monkeypatch.setattr(proxy, "validate_upstream_url", lambda url: url)

    r = await client.post(
        "/mcp",
        json=_mcp_call(CONN_NAME),
        headers={"authorization": f"Bearer {agent_token}"},
    )
    assert r.status_code == 200, r.text
    body = r.json()

    # MCP response structure
    assert "result" in body, f"expected result, got: {body}"
    result = body["result"]
    assert result.get("isError") is False, f"proxy returned error: {result}"

    # The upstream request got the injected bearer token
    assert fake.captured.get("headers", {}).get("Authorization") == f"Bearer {SECRET_KEY}"

    # URL was built correctly
    assert fake.captured["url"] == "https://api.example.com/v1/test"
    assert fake.captured["method"] == "GET"

    # Response content came through
    text = result["content"][0]["text"]
    parsed = json.loads(text)
    assert parsed["method"] == "GET"

    # Audit log was written
    row = await db.fetchrow(
        "SELECT * FROM proxy_log WHERE agent_name = $1 ORDER BY id DESC LIMIT 1",
        AGENT_NAME,
    )
    assert row is not None
    assert row["connection_name"] == CONN_NAME
    assert row["method"] == "GET"
    assert row["path"] == "/v1/test"
    assert row["upstream_status"] == 200
    assert row["account_name"] == ACCOUNT_NAME


async def test_account_token_rejected_for_proxy(client, setup):
    """An account token (not an agent token) cannot use proxy_request."""
    _, account_token = setup

    r = await client.post(
        "/mcp",
        json=_mcp_call(CONN_NAME),
        headers={"authorization": f"Bearer {account_token}"},
    )
    assert r.status_code == 200
    result = r.json()["result"]
    assert result["isError"] is True
    assert "AGENT_REQUIRED" in result["content"][0]["text"]


async def test_agent_without_grant_rejected(client, setup, db):
    """An agent without the connection in proxy_grants is denied."""
    agent_token, _ = setup

    # Remove the grant
    await db.execute(
        "UPDATE agents SET proxy_grants = $1 WHERE name = $2",
        [],
        AGENT_NAME,
    )

    r = await client.post(
        "/mcp",
        json=_mcp_call(CONN_NAME),
        headers={"authorization": f"Bearer {agent_token}"},
    )
    assert r.status_code == 200
    result = r.json()["result"]
    assert result["isError"] is True
    assert "CONNECTION_DENIED" in result["content"][0]["text"]

    # Restore grant for other tests
    await db.execute(
        "UPDATE agents SET proxy_grants = $1 WHERE name = $2",
        [CONN_NAME],
        AGENT_NAME,
    )


async def test_tools_list_includes_proxy(client, setup):
    """tools/list returns proxy_request alongside workspace tools."""
    agent_token, _ = setup

    r = await client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        headers={"authorization": f"Bearer {agent_token}"},
    )
    assert r.status_code == 200
    tools = r.json()["result"]["tools"]
    names = [t["name"] for t in tools]
    assert "proxy_request" in names

    proxy_tool = next(t for t in tools if t["name"] == "proxy_request")
    assert "connection" in proxy_tool["inputSchema"]["properties"]
    assert "method" in proxy_tool["inputSchema"]["properties"]
