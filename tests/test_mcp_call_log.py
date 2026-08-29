"""mcp_call_log — unit and e2e tests.

Unit tests cover the pure helper functions (_is_governance, _call_target)
with no database.

E2e tests POST to /mcp through the real app and query mcp_call_log to verify
rows were written with the correct shape.
"""
from __future__ import annotations

import json

import pytest
import pytest_asyncio

from datum_sync import auth, config
from datum_sync.mcp import _call_target, _is_governance

pytestmark = pytest.mark.asyncio


# -- unit: _is_governance ------------------------------------------------------


def test_governance_soul():
    assert _is_governance("SOUL.md") is True


def test_governance_skills_prefix():
    assert _is_governance("skills/python-tricks.md") is True


def test_governance_hooks_prefix():
    assert _is_governance("hooks/pre-tool.sh") is True


def test_governance_nested_skills():
    assert _is_governance("skills/sub/dir/foo.md") is True


def test_not_governance_regular_file():
    assert _is_governance("dev/notes/foo.md") is False


def test_not_governance_none():
    assert _is_governance(None) is False


def test_not_governance_memory():
    # MEMORY.md is important but not governance-class for now
    assert _is_governance("MEMORY.md") is False


# -- unit: _call_target --------------------------------------------------------


def test_target_vault_read():
    params = {"name": "vault_read", "arguments": {"path": "dev/foo.md"}}
    assert _call_target("tools/call", "vault_read", params) == "dev/foo.md"


def test_target_vault_write():
    params = {"name": "vault_write", "arguments": {"path": "skills/new.md", "content": "x"}}
    assert _call_target("tools/call", "vault_write", params) == "skills/new.md"


def test_target_proxy():
    params = {
        "name": "proxy_request",
        "arguments": {"connection": "openai", "method": "POST", "path": "/v1/chat/completions"},
    }
    assert _call_target("tools/call", "proxy_request", params) == "openai:POST:/v1/chat/completions"


def test_target_tools_list_is_none():
    assert _call_target("tools/list", None, {}) is None


def test_target_initialize_is_none():
    assert _call_target("initialize", None, {}) is None


def test_target_workspace_tool():
    params = {"name": "my_repo__my_ws", "arguments": {}}
    assert _call_target("tools/call", "my_repo__my_ws", params) == "my_repo__my_ws"


# -- e2e -----------------------------------------------------------------------

ACCOUNT_NAME = "_mcp_log_e2e_acct"
VAULT_SCOPE = {
    "read": ["dev/**", "skills/**"],
    "write": ["dev/**", "skills/**"],
    "deny": [],
}


@pytest_asyncio.fixture
async def log_setup(tmp_path, monkeypatch):
    """Account with vault scope + seeded vault + pool + ASGI client."""
    import asyncpg
    from datum_sync.api import app
    from datum_sync import db as db_module

    monkeypatch.setattr(config, "VAULT_PATH", tmp_path)
    (tmp_path / "dev").mkdir()
    (tmp_path / "dev" / "readme.md").write_text("hello")
    (tmp_path / "skills").mkdir()

    try:
        conn = await asyncpg.connect(config.DATABASE_URL, timeout=3)
    except Exception:
        pytest.skip("database unavailable")

    raw = auth.new_token()
    await conn.execute("DELETE FROM service_accounts WHERE name = $1", ACCOUNT_NAME)
    await conn.execute(
        """
        INSERT INTO service_accounts (name, token_hash, max_tier, is_admin, vault_scope)
        VALUES ($1, $2, 4, false, $3)
        """,
        ACCOUNT_NAME,
        auth.hash_token(raw),
        json.dumps(VAULT_SCOPE),
    )

    await db_module.init_pool()

    from httpx import ASGITransport, AsyncClient
    client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")

    yield {"token": raw, "client": client, "conn": conn}

    await client.aclose()
    await conn.execute("DELETE FROM mcp_call_log WHERE account_name = $1", ACCOUNT_NAME)
    await conn.execute("DELETE FROM service_accounts WHERE name = $1", ACCOUNT_NAME)
    await conn.close()
    await db_module.close_pool()


def _rpc(method, params=None, id_=1):
    return {"jsonrpc": "2.0", "id": id_, "method": method, "params": params or {}}


async def _last_log_row(conn, account_name: str) -> dict:
    row = await conn.fetchrow(
        "SELECT * FROM mcp_call_log WHERE account_name = $1 ORDER BY created_at DESC LIMIT 1",
        account_name,
    )
    assert row is not None, "no mcp_call_log row found"
    return dict(row)


# tools/list is logged
async def test_log_tools_list(log_setup):
    s = log_setup
    await s["client"].post(
        "/mcp",
        json=_rpc("tools/list"),
        headers={"Authorization": f"Bearer {s['token']}"},
    )
    row = await _last_log_row(s["conn"], ACCOUNT_NAME)
    assert row["method"] == "tools/list"
    assert row["tool_name"] is None
    assert row["target"] is None
    assert row["outcome"] == "ok"
    assert row["is_governance"] is False
    assert row["duration_ms"] >= 0


# vault_read is logged with path as target
async def test_log_vault_read(log_setup):
    s = log_setup
    await s["client"].post(
        "/mcp",
        json=_rpc("tools/call", {"name": "vault_read", "arguments": {"path": "dev/readme.md"}}),
        headers={"Authorization": f"Bearer {s['token']}"},
    )
    row = await _last_log_row(s["conn"], ACCOUNT_NAME)
    assert row["method"] == "tools/call"
    assert row["tool_name"] == "vault_read"
    assert row["target"] == "dev/readme.md"
    assert row["outcome"] == "ok"
    assert row["is_governance"] is False


# vault_write to a governance path sets is_governance
async def test_log_governance_skill_write(log_setup):
    s = log_setup
    await s["client"].post(
        "/mcp",
        json=_rpc("tools/call", {
            "name": "vault_write",
            "arguments": {"path": "skills/new-skill.md", "content": "# New skill"},
        }),
        headers={"Authorization": f"Bearer {s['token']}"},
    )
    row = await _last_log_row(s["conn"], ACCOUNT_NAME)
    assert row["tool_name"] == "vault_write"
    assert row["target"] == "skills/new-skill.md"
    assert row["is_governance"] is True
    assert row["outcome"] == "ok"


# X-Trace-Id is captured as client_trace_id
async def test_log_trace_id(log_setup):
    s = log_setup
    await s["client"].post(
        "/mcp",
        json=_rpc("tools/list"),
        headers={
            "Authorization": f"Bearer {s['token']}",
            "X-Trace-Id": "test-session-abc123",
        },
    )
    row = await _last_log_row(s["conn"], ACCOUNT_NAME)
    assert row["client_trace_id"] == "test-session-abc123"


# A missing X-Trace-Id stores NULL, not empty string
async def test_log_no_trace_id_is_null(log_setup):
    s = log_setup
    await s["client"].post(
        "/mcp",
        json=_rpc("tools/list"),
        headers={"Authorization": f"Bearer {s['token']}"},
    )
    row = await _last_log_row(s["conn"], ACCOUNT_NAME)
    assert row["client_trace_id"] is None


# Failed calls (unknown tool) are logged as error
async def test_log_error_outcome(log_setup):
    s = log_setup
    resp = await s["client"].post(
        "/mcp",
        json=_rpc("tools/call", {"name": "no_such_tool", "arguments": {}}),
        headers={"Authorization": f"Bearer {s['token']}"},
    )
    assert "error" in resp.json()
    row = await _last_log_row(s["conn"], ACCOUNT_NAME)
    assert row["outcome"] == "error"
    assert row["error_code"] is not None
