"""Principals over HTTP: narrowing, effective authority, editing rules.

spec/agent-auth-plane/03 §1-§2. Every test here drives `/rest/v1/principals`
with the conftest `token` (a tier-4 administrator named `_pytest`) and
creates its own children under it; teardown cascades from the fixture
account, which `token` drops after every test.
"""
from __future__ import annotations

import httpx
import pytest
import pytest_asyncio

from datum_sync import accounts, auth, config, db as db_module, tokens
from datum_sync.api import app
from tests.conftest import TEST_ACCOUNT

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


async def _mk(client, name, **body):
    body.setdefault("kind", "agent")
    body.setdefault("parent", TEST_ACCOUNT)
    body.setdefault("max_tier", 2)
    body.setdefault("repo_scope", ["*"])
    r = await client.post("/rest/v1/principals", json={"name": name, **body})
    return r


async def _token_for(db, name: str, label="t", max_tier=None) -> str:
    account_id = await db.fetchval("SELECT id FROM service_accounts WHERE name = $1", name)
    _, raw = await tokens.create(db, account_id, label, max_tier=max_tier)
    return raw


# -- narrowing at write ----------------------------------------------------------


async def test_a_child_wider_than_its_parent_is_refused_naming_the_field(client, db):
    """PRIN-001: `narrows()` runs at create, and the refusal says where.

    Guard: PRIN-001.
    """
    await db.execute(
        "UPDATE service_accounts SET repo_scope = ARRAY['SCIMAC'] WHERE name = $1", TEST_ACCOUNT
    )
    r = await _mk(client, "_pytest_child", repo_scope=["SCIMAC", "Other"])
    assert r.status_code == 400, r.text
    assert r.json()["code"] == "GRANT_NOT_NARROWER"
    assert r.json()["detail"]["fields"] == ["repo_scope"]
    assert await db.fetchval("SELECT count(*) FROM service_accounts WHERE name = '_pytest_child'") == 0

    r = await _mk(client, "_pytest_child", repo_scope=["SCIMAC"])
    assert r.status_code == 201, r.text
    assert r.json()["parent"] == TEST_ACCOUNT
    assert r.json()["effective_tier"] == 2


async def test_narrowing_a_parent_narrows_its_children_at_their_next_request(client, db):
    """PRIN-002: the effective grant is the meet with every ancestor.

    The child row is never written; only the parent's tier moves.

    Guard: PRIN-002.
    """
    assert (await _mk(client, "_pytest_child", max_tier=3, kind="agent")).status_code == 201
    raw = await _token_for(db, "_pytest_child")
    me = (await client.get("/rest/v1/whoami", headers=bearer(raw))).json()
    assert me["effective_tier"] == 3 and me["max_tier"] == 3

    await db.execute("UPDATE service_accounts SET max_tier = 2 WHERE name = $1", TEST_ACCOUNT)
    me = (await client.get("/rest/v1/whoami", headers=bearer(raw))).json()
    assert me["effective_tier"] == 2
    assert await db.fetchval(
        "SELECT max_tier FROM service_accounts WHERE name = '_pytest_child'"
    ) == 3, "the child row itself is untouched"


async def test_patch_refuses_is_admin(client, db):
    """PRIN-004: `is_admin` is derived from the tier and cannot be set apart from it.

    Guard: PRIN-004.
    """
    assert (await _mk(client, "_pytest_child")).status_code == 201
    r = await client.patch("/rest/v1/principals/_pytest_child", json={"is_admin": True})
    assert r.status_code == 400
    assert r.json()["detail"]["field"] == "is_admin"


async def test_a_grant_edit_that_would_leave_a_child_wider_is_refused(client, db):
    """PRIN-005: narrowing a parent below a child is refused, naming the child.

    The alternative -- silently narrowing the child through `intersect` --
    hides the change from the person making it.

    Guard: PRIN-005.
    """
    assert (await _mk(client, "_pytest_mid", kind="human", max_tier=3)).status_code == 201
    assert (await _mk(client, "_pytest_leaf", parent="_pytest_mid", max_tier=3)).status_code == 201
    r = await client.patch("/rest/v1/principals/_pytest_mid", json={"max_tier": 2})
    assert r.status_code == 409, r.text
    assert r.json()["code"] == "DESCENDANTS_WOULD_WIDEN"
    assert r.json()["detail"]["names"] == ["_pytest_leaf"]
    # Narrow the leaf first, then the parent goes through.
    assert (await client.patch("/rest/v1/principals/_pytest_leaf", json={"max_tier": 2})).status_code == 200
    assert (await client.patch("/rest/v1/principals/_pytest_mid", json={"max_tier": 2})).status_code == 200


async def test_a_tier_3_sponsor_edits_only_its_own_children(client, db):
    """PRIN-006: tier 3 creates and edits children of itself, within its own grant.

    Guard: PRIN-006.
    """
    assert (await _mk(client, "_pytest_sponsor", kind="human", max_tier=3, repo_scope=["SCIMAC"])).status_code == 201
    assert (await _mk(client, "_pytest_other", kind="agent", max_tier=2)).status_code == 201
    sponsor = await _token_for(db, "_pytest_sponsor")

    # Own child, narrower: fine, and the parent defaults to the sponsor.
    r = await client.post(
        "/rest/v1/principals",
        json={"name": "_pytest_drone", "max_tier": 2, "repo_scope": ["SCIMAC"]},
        headers=bearer(sponsor),
    )
    assert r.status_code == 201, r.text
    assert r.json()["parent"] == "_pytest_sponsor" and r.json()["kind"] == "agent"

    # Somebody else's child: not yours.
    r = await client.patch(
        "/rest/v1/principals/_pytest_other", json={"max_tier": 1}, headers=bearer(sponsor)
    )
    assert r.status_code == 403
    assert "own children" in r.json()["message"]

    # A tier-3 sponsor may not mint tier 3.
    r = await client.post(
        "/rest/v1/principals",
        json={"name": "_pytest_drone2", "max_tier": 3, "repo_scope": ["SCIMAC"]},
        headers=bearer(sponsor),
    )
    assert r.status_code == 403


async def test_an_agent_cannot_be_given_a_password(db):
    """PRIN-007: a password is a human's credential.

    Guard: PRIN-007.
    """
    await db.execute("DELETE FROM service_accounts WHERE name = '_pytest_pw_agent'")
    await db.execute(
        "INSERT INTO service_accounts (name, kind, max_tier) VALUES ('_pytest_pw_agent', 'agent', 2)"
    )
    try:
        assert await accounts.passwd("_pytest_pw_agent", "hunter2") == 1
        assert await db.fetchval(
            "SELECT password_hash FROM service_accounts WHERE name = '_pytest_pw_agent'"
        ) is None
    finally:
        await db.execute("DELETE FROM service_accounts WHERE name = '_pytest_pw_agent'")


async def test_delegation_depth_is_bounded(client, db, monkeypatch):
    """PRIN-012.

    Guard: PRIN-012.
    """
    monkeypatch.setattr(config, "POLICY_MAX_DELEGATION_DEPTH", 2)
    assert (await _mk(client, "_pytest_d1", kind="human", max_tier=3)).status_code == 201
    r = await _mk(client, "_pytest_d2", parent="_pytest_d1", max_tier=2)
    assert r.status_code == 400, r.text
    assert r.json()["code"] == "DELEGATION_TOO_DEEP"


# -- reads and tokens ----------------------------------------------------------------


async def test_the_detail_carries_ancestors_children_and_effective(client, db):
    assert (await _mk(client, "_pytest_mid", kind="human", max_tier=3)).status_code == 201
    assert (await _mk(client, "_pytest_leaf", parent="_pytest_mid", max_tier=2)).status_code == 201
    r = await client.get("/rest/v1/principals/_pytest_leaf")
    assert r.status_code == 200
    body = r.json()
    assert body["ancestors"] == ["_pytest_mid", TEST_ACCOUNT]
    assert body["effective"]["max_tier"] == 2
    r = await client.get("/rest/v1/principals/_pytest_mid")
    assert r.json()["children"] == ["_pytest_leaf"]


async def test_a_token_cap_cannot_exceed_the_principal(client, db):
    assert (await _mk(client, "_pytest_child", max_tier=2)).status_code == 201
    r = await client.post("/rest/v1/principals/_pytest_child/tokens", json={"label": "b", "max_tier": 3})
    assert r.status_code == 400 and r.json()["code"] == "TOKEN_TIER_ABOVE_PRINCIPAL"
    r = await client.post("/rest/v1/principals/_pytest_child/tokens", json={"label": "b", "max_tier": 1})
    assert r.status_code == 201 and r.json()["max_tier"] == 1 and r.json()["token"]


async def test_tier_3_lists_only_its_subtree(client, db):
    assert (await _mk(client, "_pytest_sponsor", kind="human", max_tier=3)).status_code == 201
    assert (await _mk(client, "_pytest_other", kind="agent", max_tier=2)).status_code == 201
    sponsor = await _token_for(db, "_pytest_sponsor")
    assert (await client.post("/rest/v1/principals", json={"name": "_pytest_drone", "max_tier": 2},
                              headers=bearer(sponsor))).status_code == 201
    names = {p["name"] for p in (await client.get("/rest/v1/principals", headers=bearer(sponsor))).json()["items"]}
    assert names == {"_pytest_sponsor", "_pytest_drone"}
    assert "_pytest_other" in {p["name"] for p in (await client.get("/rest/v1/principals")).json()["items"]}


async def test_delete_requires_tier_5_and_no_children(client, db):
    assert (await _mk(client, "_pytest_child", max_tier=2)).status_code == 201
    assert (await client.delete("/rest/v1/principals/_pytest_child")).status_code == 403
    await db.execute("UPDATE service_accounts SET max_tier = 5 WHERE name = $1", TEST_ACCOUNT)
    assert (await _mk(client, "_pytest_mid", kind="human", max_tier=3)).status_code == 201
    assert (await _mk(client, "_pytest_leaf", parent="_pytest_mid", max_tier=2)).status_code == 201
    r = await client.delete("/rest/v1/principals/_pytest_mid")
    assert r.status_code == 409 and r.json()["code"] == "CHILDREN_ACTIVE"
    assert (await client.delete("/rest/v1/principals/_pytest_leaf")).status_code == 200
    assert (await client.delete("/rest/v1/principals/_pytest_mid")).status_code == 200
