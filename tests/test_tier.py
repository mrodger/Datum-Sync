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
    # Jobs are text-keyed, not cascaded: a job this repository queued would
    # otherwise sit in the queue forever and make conftest's `db` fixture skip
    # every later test -- which the guard harness then reports as UNPROVEN in
    # bulk (CLAUDE.md, "a skipped test reads as a passing one"). It happened.
    await conn.execute("DELETE FROM jobs WHERE repository = $1", REPO)
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

    async def fake_run_sync(repo, ws, params, service, submitted_by=None, principal=None, **kw):
        seen.update(repo=repo, ws=ws, service=service, submitted_by=submitted_by,
                    principal=principal)
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


# -- the ceiling ---------------------------------------------------------------


async def _capped_token(conn, account_id: int, cap: int) -> str:
    _, raw = await tokens.create(conn, account_id, f"cap{cap}", max_tier=cap)
    return raw


async def test_submitting_needs_tier_3_on_every_door(tier_setup, monkeypatch):
    """TIER-001: the check is in `jobs.submit`, so REST and MCP both pay it.

    A tier-1 credential on a tier-4 principal (a baseline token) is refused
    over REST with TIER_REQUIRED and the scope that would fix it, and over
    MCP with the same code in an isError result. The worker check is
    bypassed so the MCP path reaches `jobs.submit`; nothing is queued because
    the refusal comes first.

    Guard: TIER-001.
    """
    s = tier_setup
    monkeypatch.setattr(execute, "worker_is_running", _true)
    raw = await _capped_token(s["conn"], s["account_id"], 1)

    r = await s["client"].post(
        f"/rest/v1/transformations/submit/{REPO}/{WS}",
        json={"params": {"WHO": "x"}},
        headers={"Authorization": f"Bearer {raw}"},
    )
    assert r.status_code == 403, r.text
    assert r.json()["code"] == "TIER_REQUIRED"
    assert r.json()["detail"] == {"required": 3, "effective": 1, "max_tier": 4, "elevate": "mcp:operate"}

    r = await s["client"].post(
        "/mcp",
        json=_rpc("tools/call", {"name": f"{REPO}__{WS}", "arguments": {"WHO": "x"}}),
        headers={"Authorization": f"Bearer {raw}"},
    )
    assert r.status_code == 200
    result = r.json()["result"]
    assert result["isError"] is True and "TIER_REQUIRED" in result["content"][0]["text"]
    assert await s["conn"].fetchval(
        "SELECT count(*) FROM jobs WHERE repository = $1", REPO
    ) == 0


async def _true(conn):
    return True


async def test_a_token_cap_lowers_the_effective_tier(tier_setup):
    """TIER-002.

    Guard: TIER-002.
    """
    s = tier_setup
    raw = await _capped_token(s["conn"], s["account_id"], 2)
    me = (await s["client"].get("/rest/v1/whoami", headers={"Authorization": f"Bearer {raw}"})).json()
    assert me["max_tier"] == 4 and me["token_tier_cap"] == 2 and me["effective_tier"] == 2
    assert me["elevate"] == {
        "mcp:operate": "unlocks tier 3: " + auth_tier_verbs()[3],
        "mcp:admin": "unlocks tier 4: " + auth_tier_verbs()[4],
    }


def auth_tier_verbs():
    from datum_sync import auth
    return auth.TIER_VERBS


async def test_an_oauth_scope_caps_the_effective_tier(tier_setup):
    """TIER-003: `mcp` caps at 2 whatever the principal's tier.

    Guard: TIER-003.
    """
    from datum_sync import auth

    s = tier_setup
    conn = s["conn"]
    await conn.execute(
        """
        INSERT INTO oauth_clients (client_id, client_name, redirect_uris)
        VALUES ('_pytest_tier_client', 'tier test', ARRAY['https://example.test/cb'])
        ON CONFLICT (client_id) DO NOTHING
        """
    )
    raw = auth.new_token()
    await conn.execute(
        """
        INSERT INTO oauth_tokens (token_hash, kind, client_id, account_id, resource, scope, expires_at)
        VALUES ($1, 'access', '_pytest_tier_client', $2, $3, 'mcp', now() + interval '1 hour')
        """,
        auth.hash_token(raw), s["account_id"], auth.MCP_RESOURCE,
    )
    try:
        me = (await s["client"].get("/rest/v1/whoami", headers={"Authorization": f"Bearer {raw}"})).json()
        assert me["scope"] == "mcp" and me["effective_tier"] == 2 and me["max_tier"] == 4
    finally:
        await conn.execute("DELETE FROM oauth_clients WHERE client_id = '_pytest_tier_client'")


async def test_the_catalogue_hides_what_the_tier_cannot_call(tier_setup):
    """TIER-004: fewer tools rather than broken ones.

    Guard: TIER-004.
    """
    s = tier_setup
    full = (await s["client"].post("/mcp", json=_rpc("tools/list"),
                                   headers={"Authorization": f"Bearer {s['token']}"})).json()
    assert f"{REPO}__{WS}" in {t["name"] for t in full["result"]["tools"]}
    raw = await _capped_token(s["conn"], s["account_id"], 2)
    capped = (await s["client"].post("/mcp", json=_rpc("tools/list"),
                                     headers={"Authorization": f"Bearer {raw}"})).json()
    assert f"{REPO}__{WS}" not in {t["name"] for t in capped["result"]["tools"]}


async def test_vault_write_needs_tier_3(tier_setup, monkeypatch, tmp_path):
    """TIER-006: a scoped, capped token can read but not write.

    Guard: TIER-006.
    """
    import json as _json

    from datum_sync import config

    s = tier_setup
    monkeypatch.setattr(config, "VAULT_PATH", tmp_path)
    (tmp_path / "dev").mkdir()
    (tmp_path / "dev" / "a.md").write_text("hi")
    await s["conn"].execute(
        "UPDATE service_accounts SET vault_scope = $2 WHERE id = $1",
        s["account_id"], _json.dumps({"read": ["dev/**"], "write": ["dev/**"]}),
    )
    raw = await _capped_token(s["conn"], s["account_id"], 2)
    h = {"Authorization": f"Bearer {raw}"}
    listed = (await s["client"].post("/mcp", json=_rpc("tools/list"), headers=h)).json()
    names = {t["name"] for t in listed["result"]["tools"]}
    assert "vault_read" in names and "vault_write" not in names
    r = (await s["client"].post("/mcp", json=_rpc("tools/call", {
        "name": "vault_write", "arguments": {"path": "dev/b.md", "content": "x"}}), headers=h)).json()
    assert r["result"]["isError"] and "TIER_REQUIRED" in r["result"]["content"][0]["text"]
    assert not (tmp_path / "dev" / "b.md").exists()
    r = (await s["client"].post("/mcp", json=_rpc("tools/call", {
        "name": "vault_read", "arguments": {"path": "dev/a.md"}}), headers=h)).json()
    assert r["result"]["isError"] is False
