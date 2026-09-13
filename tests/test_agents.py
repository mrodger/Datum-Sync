"""Agent identity and token resolution tests."""
from __future__ import annotations

import httpx
import pytest
import pytest_asyncio

from conftest import TEST_ACCOUNT

from datum_sync import agents, auth, db as db_module, tokens
from datum_sync.api import app

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def client(db, token):
    await db_module.init_pool()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
        headers={"authorization": f"Bearer {token}"},
    ) as c:
        yield c
    await db_module.close_pool()


def bearer(raw: str) -> dict[str, str]:
    return {"authorization": f"Bearer {raw}"}


# -- CRUD via API ----------------------------------------------------------


async def test_create_agent(client, db):
    r = await client.post(
        f"/rest/v1/accounts/{TEST_ACCOUNT}/agents",
        json={"name": "test-agent", "proxy_grants": ["openrouter"]},
    )
    assert r.status_code == 201
    body = r.json()
    assert body["name"] == "test-agent"
    assert body["proxy_grants"] == ["openrouter"]
    assert "token" in body, "token must be returned on create"
    # Cleanup
    await db.execute("DELETE FROM agents WHERE name = 'test-agent'")


async def test_create_agent_duplicate(client, db):
    await client.post(
        f"/rest/v1/accounts/{TEST_ACCOUNT}/agents",
        json={"name": "dup-agent"},
    )
    r = await client.post(
        f"/rest/v1/accounts/{TEST_ACCOUNT}/agents",
        json={"name": "dup-agent"},
    )
    assert r.status_code == 409
    await db.execute("DELETE FROM agents WHERE name = 'dup-agent'")


async def test_list_agents(client, db):
    await client.post(
        f"/rest/v1/accounts/{TEST_ACCOUNT}/agents",
        json={"name": "list-a"},
    )
    await client.post(
        f"/rest/v1/accounts/{TEST_ACCOUNT}/agents",
        json={"name": "list-b"},
    )
    r = await client.get(f"/rest/v1/accounts/{TEST_ACCOUNT}/agents")
    assert r.status_code == 200
    names = [a["name"] for a in r.json()["items"]]
    assert "list-a" in names
    assert "list-b" in names
    await db.execute("DELETE FROM agents WHERE name LIKE 'list-%'")


async def test_get_agent(client, db):
    await client.post(
        f"/rest/v1/accounts/{TEST_ACCOUNT}/agents",
        json={"name": "get-agent", "proxy_grants": ["tavily"]},
    )
    r = await client.get(f"/rest/v1/accounts/{TEST_ACCOUNT}/agents/get-agent")
    assert r.status_code == 200
    assert r.json()["proxy_grants"] == ["tavily"]
    await db.execute("DELETE FROM agents WHERE name = 'get-agent'")


async def test_update_grants(client, db):
    await client.post(
        f"/rest/v1/accounts/{TEST_ACCOUNT}/agents",
        json={"name": "upd-agent", "proxy_grants": ["a"]},
    )
    r = await client.patch(
        f"/rest/v1/accounts/{TEST_ACCOUNT}/agents/upd-agent",
        json={"proxy_grants": ["a", "b", "c"]},
    )
    assert r.status_code == 200
    assert r.json()["proxy_grants"] == ["a", "b", "c"]
    await db.execute("DELETE FROM agents WHERE name = 'upd-agent'")


async def test_mint_agent_token(client, db):
    create = await client.post(
        f"/rest/v1/accounts/{TEST_ACCOUNT}/agents",
        json={"name": "mint-agent"},
    )
    old_token = create.json()["token"]

    r = await client.post(
        f"/rest/v1/accounts/{TEST_ACCOUNT}/agents/mint-agent/token"
    )
    assert r.status_code == 200
    new_token = r.json()["token"]
    assert new_token != old_token

    # Old token should no longer resolve
    with pytest.raises(auth.ApiError) as exc_info:
        await auth.resolve(db, old_token)
    assert exc_info.value.status == 401

    await db.execute("DELETE FROM agents WHERE name = 'mint-agent'")


async def test_delete_agent(client, db):
    await client.post(
        f"/rest/v1/accounts/{TEST_ACCOUNT}/agents",
        json={"name": "del-agent"},
    )
    r = await client.delete(
        f"/rest/v1/accounts/{TEST_ACCOUNT}/agents/del-agent"
    )
    assert r.status_code == 200
    assert r.json()["deleted"] is True

    r = await client.get(f"/rest/v1/accounts/{TEST_ACCOUNT}/agents/del-agent")
    assert r.status_code == 404


# -- token resolution ------------------------------------------------------


async def test_agent_token_resolves_to_principal(db):
    """An agent token resolves to the agent's own principal, under its parent.

    Since migration 017 an agent is a `service_accounts` row of kind 'agent'.
    Its token resolves to *that* row -- its own name, its own grants -- with
    the parent folded in by `auth.effective`. The parent here is admin, and
    the agent is not: `kind = 'agent'` implies tier <= 3 (a CHECK from 016),
    and `is_admin` is derived from the tier.

    Guard: AGENT-001.
    """
    account_id = await db.fetchval(
        """
        INSERT INTO service_accounts (name, max_tier, is_admin)
        VALUES ('_agent_test_acct', 4, true)
        RETURNING id
        """
    )
    _, raw = await agents.create(db, account_id, "_agent_test", ["openrouter", "tavily"])

    try:
        principal = await auth.resolve(db, raw)
        assert principal.kind == "agent"
        assert principal.parent_id == account_id
        assert principal.agent_id == principal.account_id
        assert principal.agent_name == "_agent_test"
        assert principal.name == "_agent_test"
        assert sorted(principal.proxy_grants) == ["openrouter", "tavily"]
        assert principal.max_tier == 3
        assert principal.effective_tier == 3
        assert principal.is_admin is False  # agents are never admin
        assert principal.source == "token"
    finally:
        await db.execute("DELETE FROM service_accounts WHERE name = '_agent_test_acct'")


async def test_disabled_agent_rejected(db):
    account_id = await db.fetchval(
        """
        INSERT INTO service_accounts (name, max_tier, is_admin)
        VALUES ('_dis_agent_acct', 2, false)
        RETURNING id
        """
    )
    _, raw = await agents.create(db, account_id, "_dis_agent")
    await agents.disable(db, "_dis_agent", True)
    try:
        with pytest.raises(auth.ApiError) as exc_info:
            await auth.resolve(db, raw)
        assert exc_info.value.status == 401
        assert "disabled" in exc_info.value.message.lower()
    finally:
        await db.execute("DELETE FROM service_accounts WHERE name = '_dis_agent_acct'")


async def test_disabled_account_blocks_agent(db):
    """Disabling the sponsor disables its agents at their next request.

    Guard: PRIN-003.
    """
    account_id = await db.fetchval(
        """
        INSERT INTO service_accounts (name, max_tier, is_admin)
        VALUES ('_dis_acct2', 2, false)
        RETURNING id
        """
    )
    _, raw = await agents.create(db, account_id, "_dis_acct_agent")
    await db.execute("UPDATE service_accounts SET disabled = true WHERE id = $1", account_id)
    try:
        with pytest.raises(auth.ApiError) as exc_info:
            await auth.resolve(db, raw)
        assert exc_info.value.status == 401
        assert exc_info.value.code == "ANCESTOR_DISABLED"
    finally:
        await db.execute("DELETE FROM service_accounts WHERE name = '_dis_acct2'")


async def test_account_token_has_no_agent_id(db):
    """A plain account token resolves as a human with no agent identity."""
    account_id = await db.fetchval(
        """
        INSERT INTO service_accounts (name, max_tier, is_admin)
        VALUES ('_no_agent_acct', 4, true)
        RETURNING id
        """
    )
    _, raw = await tokens.create(db, account_id, "fixture")
    try:
        principal = await auth.resolve(db, raw)
        assert principal.kind == "human"
        assert principal.agent_id is None
        assert principal.agent_name is None
        assert principal.proxy_grants == []
    finally:
        await db.execute("DELETE FROM service_accounts WHERE name = '_no_agent_acct'")


async def test_agent_cascade_on_account_delete(db):
    """Deleting an account cascades to its agents."""
    account_id = await db.fetchval(
        """
        INSERT INTO service_accounts (name, max_tier, is_admin)
        VALUES ('_cascade_acct', 1, false)
        RETURNING id
        """
    )
    await db.execute(
        "INSERT INTO agents (account_id, name, token_hash) VALUES ($1, '_cascade_agent', $2)",
        account_id,
        auth.hash_token("agent-tok"),
    )
    await db.execute("DELETE FROM service_accounts WHERE name = '_cascade_acct'")

    count = await db.fetchval("SELECT count(*) FROM agents WHERE name = '_cascade_agent'")
    assert count == 0
