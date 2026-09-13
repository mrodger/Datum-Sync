"""Lifecycle: the state machine, and what each state authenticates as.

spec/agent-auth-plane/03 §4. Driven over `/rest/v1/principals/{name}/...`
with the conftest administrator, plus direct calls to `lifecycle.daily`.
"""
from __future__ import annotations

import httpx
import pytest
import pytest_asyncio

from datum_sync import config, db as db_module, lifecycle, tokens
from datum_sync.api import app
from tests.conftest import TEST_ACCOUNT

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def client(db, token):
    await db_module.init_pool()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver",
        headers={"authorization": f"Bearer {token}"},
    ) as c:
        yield c
    await db_module.close_pool()


def bearer(raw: str) -> dict[str, str]:
    return {"authorization": f"Bearer {raw}"}


async def _agent(client, db, name="_pytest_lc", tier=3, state="active", **extra):
    r = await client.post("/rest/v1/principals", json={
        "name": name, "kind": "agent", "parent": TEST_ACCOUNT, "max_tier": tier,
        "repo_scope": ["*"], "proxy_grants": [], **extra,
    })
    assert r.status_code == 201, r.text
    if state != "active":
        await db.execute("UPDATE service_accounts SET state = $2 WHERE name = $1", name, state)
    account_id = await db.fetchval("SELECT id FROM service_accounts WHERE name = $1", name)
    _, raw = await tokens.create(db, account_id, "t")
    return raw


async def test_restricted_is_forced_to_tier_1(client, db):
    """PRIN-008: whatever the row says, a restricted principal acts at tier 1.

    Guard: PRIN-008.
    """
    await db.execute(
        "UPDATE service_accounts SET proxy_grants = ARRAY['x'], vault_scope = $2 WHERE name = $1",
        TEST_ACCOUNT, '{"read": ["dev/**"], "write": ["dev/**"]}',
    )
    raw = await _agent(client, db, proxy_grants=["x"],
                       vault_scope={"read": ["dev/**"], "write": ["dev/**"]})
    me = (await client.get("/rest/v1/whoami", headers=bearer(raw))).json()
    assert me["effective_tier"] == 3 and me["proxy_grants"] == ["x"]

    r = await client.post("/rest/v1/principals/_pytest_lc/restrict", json={"reason": "test"})
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "restricted"

    me = (await client.get("/rest/v1/whoami", headers=bearer(raw))).json()
    assert me["state"] == "restricted"
    assert me["effective_tier"] == 1 and me["max_tier"] == 1
    assert me["proxy_grants"] == []
    assert me["vault_scope"] == {"read": ["dev/**"]}
    assert me["limits"]["concurrent_sessions"] == 1
    assert await db.fetchval(
        "SELECT max_tier FROM service_accounts WHERE name = '_pytest_lc'"
    ) == 3, "the row keeps its tier; only the effective grant is forced"


@pytest.mark.parametrize("state", ["pending", "retired", "rejected"])
async def test_non_active_states_do_not_authenticate(client, db, state):
    """PRIN-009: 401 with the state as the code.

    Guard: PRIN-009.
    """
    raw = await _agent(client, db, state=state)
    r = await client.get("/rest/v1/whoami", headers=bearer(raw))
    assert r.status_code == 401
    assert r.json()["code"] == f"PRINCIPAL_{state.upper()}"


async def test_restore_puts_back_exactly_what_was_stored(client, db):
    """PRIN-010: the snapshot, not the live row.

    The live row is edited between restrict and restore, the way a hand
    edit or a later PATCH might; restore must not carry that edit.

    Guard: PRIN-010.
    """
    raw = await _agent(client, db)
    assert (await client.post("/rest/v1/principals/_pytest_lc/restrict",
                              json={"reason": "r"})).status_code == 200
    await db.execute("UPDATE service_accounts SET repo_scope = '{}' WHERE name = '_pytest_lc'")
    r = await client.post("/rest/v1/principals/_pytest_lc/restore")
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "active"
    assert r.json()["repo_scope"] == ["*"]
    assert r.json()["restricted_reason"] is None
    me = (await client.get("/rest/v1/whoami", headers=bearer(raw))).json()
    assert me["effective_tier"] == 3 and me["repo_scope"] == ["*"]


async def test_retire_revokes_every_credential_and_keeps_the_row(client, db):
    """PRIN-011.

    Guard: PRIN-011.
    """
    raw = await _agent(client, db)
    assert (await client.get("/rest/v1/whoami", headers=bearer(raw))).status_code == 200
    r = await client.post("/rest/v1/principals/_pytest_lc/retire")
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "retired" and r.json()["retired"] == ["_pytest_lc"]
    assert await db.fetchval(
        "SELECT count(*) FROM account_tokens t JOIN service_accounts s ON s.id = t.account_id "
        "WHERE s.name = '_pytest_lc' AND t.revoked_at IS NULL"
    ) == 0
    assert await db.fetchval("SELECT count(*) FROM service_accounts WHERE name = '_pytest_lc'") == 1
    r = await client.get("/rest/v1/whoami", headers=bearer(raw))
    assert r.status_code == 401 and r.json()["code"] == "TOKEN_REVOKED"


async def test_retire_needs_the_children_first_unless_cascade(client, db):
    await _agent(client, db, name="_pytest_mid", tier=3)
    r = await client.post("/rest/v1/principals", json={
        "name": "_pytest_leaf", "kind": "agent", "parent": "_pytest_mid", "max_tier": 2,
        "repo_scope": ["*"]})
    assert r.status_code == 201, r.text
    r = await client.post("/rest/v1/principals/_pytest_mid/retire")
    assert r.status_code == 409 and r.json()["code"] == "CHILDREN_ACTIVE"
    r = await client.post("/rest/v1/principals/_pytest_mid/retire?cascade=true")
    assert r.status_code == 200 and sorted(r.json()["retired"]) == ["_pytest_leaf", "_pytest_mid"]


async def test_invalid_transitions_are_refused_loudly(client, db):
    await _agent(client, db)
    r = await client.post("/rest/v1/principals/_pytest_lc/restore")
    assert r.status_code == 409 and r.json()["code"] == "INVALID_TRANSITION"
    r = await client.post("/rest/v1/principals/_pytest_lc/approve")
    assert r.status_code == 409


async def test_a_review_may_narrow_but_never_widen(client, db):
    await _agent(client, db, tier=3)
    r = await client.post("/rest/v1/principals/_pytest_lc/review",
                          json={"narrow": {"max_tier": 4}, "note": "x"})
    assert r.status_code == 400 and r.json()["code"] == "REVIEW_MUST_NARROW"
    r = await client.post("/rest/v1/principals/_pytest_lc/review",
                          json={"narrow": {"max_tier": 2}, "note": "quarterly"})
    assert r.status_code == 200, r.text
    assert r.json()["max_tier"] == 2
    assert r.json()["last_reviewed_by"] == TEST_ACCOUNT
    assert r.json()["review_due_at"] is not None
    assert r.json()["metadata"]["last_review_note"] == "quarterly"


async def test_the_daily_tick_expires_pending_and_restricts_idle_agents(client, db):
    """Idle agents are restricted; humans are never touched (D-20)."""
    await _agent(client, db, name="_pytest_stale", state="pending")
    await _agent(client, db, name="_pytest_idle")
    await db.execute("""
        UPDATE service_accounts SET created_at = now() - interval '30 days',
               last_used_at = now() - interval '40 days'
         WHERE name IN ('_pytest_stale', '_pytest_idle')
    """)
    # A human just as idle.
    await db.execute("""
        UPDATE service_accounts SET last_used_at = now() - interval '400 days'
         WHERE name = $1
    """, TEST_ACCOUNT)
    done = await lifecycle.daily(db)
    assert done["expired"] == ["_pytest_stale"]
    assert done["restricted"] == ["_pytest_idle"]
    assert await db.fetchval(
        "SELECT state FROM service_accounts WHERE name = '_pytest_stale'") == "rejected"
    assert await db.fetchval(
        "SELECT state FROM service_accounts WHERE name = '_pytest_idle'") == "restricted"
    assert await db.fetchval(
        "SELECT state FROM service_accounts WHERE name = $1", TEST_ACCOUNT) == "active"
    row = await db.fetchrow(
        "SELECT actor_kind, actor_name, verb FROM audit_log WHERE target = '_pytest_idle' "
        "ORDER BY id DESC LIMIT 1")
    assert row["actor_kind"] == "system" and row["verb"] == "principal.auto_restrict"
    # Idempotent.
    again = await lifecycle.daily(db)
    assert again["expired"] == [] and again["restricted"] == []


async def test_health_counts_review_obligations(client, db):
    await _agent(client, db)
    await db.execute("UPDATE service_accounts SET review_due_at = now() - interval '1 day' "
                     "WHERE name = '_pytest_lc'")
    await _agent(client, db, name="_pytest_pend", state="pending")
    h = (await client.get("/health")).json()
    assert h["reviews_overdue"] >= 1 and h["pending_enrolments"] >= 1
