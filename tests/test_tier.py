"""Tier as a verb ceiling, and the attribution that goes with it.

Started in WP0 of spec/agent-auth-plane with one test; WP1 adds the ceiling
itself. Fixtures here build their own account and workspace rows rather than
reusing conftest's, because these tests need a workspace that publishes the
`data_streaming` service (so MCP lists it) and an account whose name is
distinct from every other fixture's, so the rows they assert on are theirs.
"""
from __future__ import annotations

import json
import uuid

import asyncpg
import pytest
import pytest_asyncio

from datum_sync import config, execute, tokens

ACCOUNT = "_pytest_tier"
REPO = "_pytest_tier"
WS = "streams"

MANIFEST = {
    "name": WS,
    "description": "fixture",
    "version": "0.1.0",
    "parameters": [{"name": "WHO", "type": "STRING", "default": "x"}],
    "outputs": [{"name": "out", "type": "text/plain", "primary": True}],
    "services": ["job_submitter", "data_streaming"],
    "timeout_seconds": 30,
}


@pytest_asyncio.fixture
async def tier_setup():
    from datum_sync import db as db_module
    from datum_sync.api import app

    try:
        conn = await asyncpg.connect(config.DATABASE_URL, timeout=3)
    except Exception:
        pytest.skip("database unavailable")

    await conn.execute("DELETE FROM service_accounts WHERE name = $1", ACCOUNT)
    await conn.execute("DELETE FROM repositories WHERE name = $1", REPO)
    account_id = await conn.fetchval(
        """
        INSERT INTO service_accounts (name, max_tier, is_admin, repo_scope)
        VALUES ($1, 4, false, ARRAY['*']) RETURNING id
        """,
        ACCOUNT,
    )
    _, raw = await tokens.create(conn, account_id, "fixture")
    repo_id = await conn.fetchval(
        "INSERT INTO repositories (name, path) VALUES ($1, '/nonexistent') RETURNING id",
        REPO,
    )
    await conn.execute(
        """
        INSERT INTO workspaces (repository_id, name, version, manifest)
        VALUES ($1, $2, '0.1.0', $3)
        """,
        repo_id,
        WS,
        json.dumps(MANIFEST),
    )

    await db_module.init_pool()
    from httpx import ASGITransport, AsyncClient

    client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    yield {"token": raw, "client": client, "conn": conn, "account_id": account_id}

    await client.aclose()
    await conn.execute("DELETE FROM mcp_call_log WHERE account_name = $1", ACCOUNT)
    await conn.execute("DELETE FROM audit_log WHERE actor_name = $1", ACCOUNT)
    await conn.execute("DELETE FROM repositories WHERE name = $1", REPO)
    await conn.execute("DELETE FROM service_accounts WHERE name = $1", ACCOUNT)
    await conn.close()
    await db_module.close_pool()


def _rpc(method, params=None, id_=1):
    return {"jsonrpc": "2.0", "id": id_, "method": method, "params": params or {}}


async def test_an_mcp_tool_call_records_who_submitted_the_job(tier_setup, monkeypatch):
    """The job row names the principal, not just the audit row.

    `run_sync` is intercepted rather than run: it needs a live worker, and
    the property under test is what MCP hands it, not what the worker does
    with it. The fake returns a row shaped like the one `_content_blocks`
    reads.

    Guard: TIER-011.
    """
    seen: dict = {}

    async def fake_run_sync(repo, ws, params, service, submitted_by=None):
        seen.update(repo=repo, ws=ws, service=service, submitted_by=submitted_by)
        return {"id": uuid.uuid4(), "artifacts": "[]"}, None

    monkeypatch.setattr(execute, "run_sync", fake_run_sync)

    s = tier_setup
    r = await s["client"].post(
        "/mcp",
        json=_rpc("tools/call", {"name": f"{REPO}__{WS}", "arguments": {"WHO": "me"}}),
        headers={"Authorization": f"Bearer {s['token']}"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["result"]["isError"] is False
    assert seen["repo"] == REPO and seen["ws"] == WS
    assert seen["submitted_by"] == ACCOUNT
