"""Agent identity and token resolution tests."""
from __future__ import annotations

import httpx
import pytest
import pytest_asyncio

from conftest import TEST_ACCOUNT

from datum_sync import auth, db as db_module, tokens
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
    """An agent token resolves to a Principal with agent_id set.

    The parent account is deliberately admin so the ``is_admin is False``
    assertion below proves that agents are NEVER admin regardless of their
    parent's status.

    Guard: AGENT-001.
    """
    # Create an admin account — agents must still resolve as non-admin.
    account_id = await db.fetchval(
        """
        INSERT INTO service_accounts (name, max_tier, is_admin)
        VALUES ('_agent_test_acct', 3, true)
        RETURNING id
        """
    )

    # Create an agent
    raw = auth.new_token()
    await db.execute(
        """
        INSERT INTO agents (account_id, name, token_hash, proxy_grants)
        VALUES ($1, '_agent_test', $2, $3)
        """,
        account_id,
        auth.hash_token(raw),
        ["openrouter", "tavily"],
    )

    try:
        principal = await auth.resolve(db, raw)
        assert principal.agent_id is not None
        assert principal.agent_name == "_agent_test"
        assert principal.proxy_grants == ["openrouter", "tavily"]
        assert principal.name == "_agent_test_acct"  # parent account name
        assert principal.max_tier == 3
        assert principal.is_admin is False  # agents are never admin
        assert principal.source == "agent"
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
    raw = auth.new_token()
    await db.execute(
        """
        INSERT INTO agents (account_id, name, token_hash, disabled)
        VALUES ($1, '_dis_agent', $2, true)
        """,
        account_id,
        auth.hash_token(raw),
    )
    try:
        with pytest.raises(auth.ApiError) as exc_info:
            await auth.resolve(db, raw)
        assert exc_info.value.status == 401
        assert "disabled" in exc_info.value.message.lower()
    finally:
        await db.execute("DELETE FROM service_accounts WHERE name = '_dis_agent_acct'")


async def test_disabled_account_blocks_agent(db):
    account_id = await db.fetchval(
        """
        INSERT INTO service_accounts (name, max_tier, is_admin, disabled)
        VALUES ('_dis_acct2', 2, false, true)
        RETURNING id
        """
    )
    raw = auth.new_token()
    await db.execute(
        """
        INSERT INTO agents (account_id, name, token_hash)
        VALUES ($1, '_dis_acct_agent', $2)
        """,
        account_id,
        auth.hash_token(raw),
    )
    try:
        with pytest.raises(auth.ApiError) as exc_info:
            await auth.resolve(db, raw)
        assert exc_info.value.status == 401
    finally:
        await db.execute("DELETE FROM service_accounts WHERE name = '_dis_acct2'")


async def test_account_token_has_no_agent_id(db):
    """A plain account token resolves with agent_id=None."""
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
        assert principal.agent_id is None
        assert principal.agent_name is None
        assert principal.proxy_grants is None
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
