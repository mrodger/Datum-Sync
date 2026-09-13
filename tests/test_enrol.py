"""Enrolment: a code in, a principal out, a token once.

spec/agent-auth-plane/03 §4.1. `anon` is a client with no credential: the
enrolment endpoints are public and the code is what authenticates.
"""
from __future__ import annotations

import httpx
import pytest
import pytest_asyncio

from datum_sync import auth, config, db as db_module
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


@pytest_asyncio.fixture
async def anon(client):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c


@pytest_asyncio.fixture(autouse=True)
async def _clean(db):
    auth.reset_attempts()
    yield
    auth.reset_attempts()
    await db.execute("DELETE FROM service_accounts WHERE name LIKE 'pytest-enrol%'")


TEMPLATE = {"max_tier": 2, "repo_scope": ["*"], "proxy_grants": [], "limits": {"jobs_per_hour": 10},
            "rate_limit_per_min": None}


async def _code(client, **body):
    payload = {"template": TEMPLATE, "label": "test", "max_uses": 1, "expires_in_hours": 1}
    payload.update(body)
    r = await client.post("/rest/v1/enrolment/codes", json=payload)
    assert r.status_code == 201, r.text
    return r.json()


async def test_enrol_approve_claim(client, anon, db):
    """The whole path: pending, refused claim, approval, one token."""
    code = await _code(client)
    r = await anon.post("/enrol", json={"code": code["code"], "name": "pytest-enrol-a",
                                        "metadata": {"model": "x", "host": "here"}})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["state"] == "pending" and body["parent"] == TEST_ACCOUNT and body["claim_code"]
    assert "token" not in body

    r = await anon.post("/enrol/claim", json={"claim_code": body["claim_code"]})
    assert r.status_code == 409 and r.json()["code"] == "PRINCIPAL_PENDING"

    pend = (await client.get("/rest/v1/enrolment/pending")).json()["items"]
    assert [p["name"] for p in pend] == ["pytest-enrol-a"]
    assert pend[0]["metadata"] == {"model": "x", "host": "here"}
    assert pend[0]["template"]["max_tier"] == 2

    r = await client.post("/rest/v1/principals/pytest-enrol-a/approve",
                          json={"edits": {"max_tier": 1}})
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "active" and r.json()["max_tier"] == 1
    assert r.json()["review_due_at"] is not None

    r = await anon.post("/enrol/claim", json={"claim_code": body["claim_code"]})
    assert r.status_code == 200, r.text
    claimed = r.json()
    assert claimed["token"] and claimed["token_tier_cap"] == 1
    assert claimed["whoami"]["kind"] == "agent" and claimed["whoami"]["effective_tier"] == 1
    me = (await client.get("/rest/v1/whoami",
                           headers={"authorization": f"Bearer {claimed['token']}"})).json()
    assert me["name"] == "pytest-enrol-a" and me["parent_id"] is not None


async def test_a_code_is_counted_and_expires(client, anon, db):
    """ENRL-001.

    Guard: ENRL-001.
    """
    code = await _code(client, max_uses=1)
    r = await anon.post("/enrol", json={"code": code["code"], "name": "pytest-enrol-1"})
    assert r.status_code == 201
    r = await anon.post("/enrol", json={"code": code["code"], "name": "pytest-enrol-2"})
    assert r.status_code == 401 and r.json()["code"] == "ENROL_CODE_INVALID"

    code = await _code(client, max_uses=5)
    await db.execute("UPDATE registration_codes SET expires_at = now() - interval '1 minute' "
                     "WHERE id = $1", code["id"])
    r = await anon.post("/enrol", json={"code": code["code"], "name": "pytest-enrol-3"})
    assert r.status_code == 401
    listed = (await client.get("/rest/v1/enrolment/codes")).json()["items"]
    assert not next(c for c in listed if c["id"] == code["id"])["live"]


async def test_the_request_must_narrow_the_template(client, anon):
    """ENRL-002.

    Guard: ENRL-002.
    """
    code = await _code(client)
    r = await anon.post("/enrol", json={"code": code["code"], "name": "pytest-enrol-w",
                                        "requested": {"max_tier": 3}})
    assert r.status_code == 400 and r.json()["code"] == "GRANT_NOT_NARROWER"
    assert r.json()["detail"]["fields"] == ["max_tier"]
    r = await anon.post("/enrol", json={"code": code["code"], "name": "pytest-enrol-n",
                                        "requested": {"max_tier": 1, "repo_scope": ["SCIMAC"]}})
    assert r.status_code == 201, r.text
    pend = (await client.get("/rest/v1/enrolment/pending")).json()["items"]
    assert pend[0]["requested"]["repo_scope"] == ["SCIMAC"] and pend[0]["repo_scope"] == ["SCIMAC"]


async def test_a_claim_code_is_single_use_and_expires(client, anon, db):
    """ENRL-003.

    Guard: ENRL-003.
    """
    code = await _code(client, auto_approve=True)
    r = await anon.post("/enrol", json={"code": code["code"], "name": "pytest-enrol-c"})
    assert r.status_code == 201 and r.json()["state"] == "active" and r.json()["token"]
    # Auto-approve claimed on the spot; the claim row is spent.
    claim_hash = await db.fetchval(
        "SELECT cl.claim_hash FROM registration_claims cl JOIN service_accounts s "
        "ON s.id = cl.principal_id WHERE s.name = 'pytest-enrol-c'")
    assert claim_hash
    # Replaying is refused. We do not have the raw claim (it was consumed
    # server-side), so mint a fresh pending one and spend it twice.
    code2 = await _code(client)
    r = await anon.post("/enrol", json={"code": code2["code"], "name": "pytest-enrol-d"})
    claim = r.json()["claim_code"]
    assert (await client.post("/rest/v1/principals/pytest-enrol-d/approve")).status_code == 200
    assert (await anon.post("/enrol/claim", json={"claim_code": claim})).status_code == 200
    r = await anon.post("/enrol/claim", json={"claim_code": claim})
    assert r.status_code == 401 and r.json()["code"] == "CLAIM_INVALID"
    # And an expired one.
    code3 = await _code(client)
    r = await anon.post("/enrol", json={"code": code3["code"], "name": "pytest-enrol-e"})
    claim = r.json()["claim_code"]
    assert (await client.post("/rest/v1/principals/pytest-enrol-e/approve")).status_code == 200
    await db.execute("UPDATE registration_claims SET expires_at = now() - interval '1 second' "
                     "WHERE claim_hash = $1", auth.hash_token(claim))
    assert (await anon.post("/enrol/claim", json={"claim_code": claim})).status_code == 401


async def test_enrol_is_rate_limited_per_code(client, anon, monkeypatch):
    """ENRL-004: the password lockout, keyed on the code.

    Guard: ENRL-004.
    """
    monkeypatch.setattr(config, "PASSWORD_MAX_ATTEMPTS", 3)
    for _ in range(3):
        r = await anon.post("/enrol", json={"code": "nope", "name": "pytest-enrol-x"})
        assert r.status_code == 401
    r = await anon.post("/enrol", json={"code": "nope", "name": "pytest-enrol-x"})
    assert r.status_code == 429 and r.headers["retry-after"]
    # Another code is another bucket.
    r = await anon.post("/enrol", json={"code": "other", "name": "pytest-enrol-x"})
    assert r.status_code == 401


async def test_auto_approve_codes_need_tier_4(client, db):
    """ENRL-005.

    Guard: ENRL-005.
    """
    r = await client.post("/rest/v1/principals", json={
        "name": "pytest-enrol-sponsor", "kind": "human", "parent": TEST_ACCOUNT,
        "max_tier": 3, "repo_scope": ["*"]})
    assert r.status_code == 201, r.text
    from datum_sync import tokens
    sponsor_id = await db.fetchval("SELECT id FROM service_accounts WHERE name = 'pytest-enrol-sponsor'")
    _, sponsor = await tokens.create(db, sponsor_id, "t")
    h = {"authorization": f"Bearer {sponsor}"}
    r = await client.post("/rest/v1/enrolment/codes",
                          json={"template": TEMPLATE, "auto_approve": True}, headers=h)
    assert r.status_code == 403 and r.json()["code"] == "TIER_REQUIRED"
    r = await client.post("/rest/v1/enrolment/codes", json={"template": TEMPLATE}, headers=h)
    assert r.status_code == 201 and r.json()["parent"] == "pytest-enrol-sponsor"
    # And a tier-3 sponsor's template must narrow the sponsor.
    # A tier-3 sponsor hands out tier <= 2 (03 §2), as when creating a child.
    r = await client.post("/rest/v1/enrolment/codes",
                          json={"template": {**TEMPLATE, "max_tier": 3, "repo_scope": ["*"]}},
                          headers=h)
    assert r.status_code == 403, r.text


async def test_names_are_validated(client, anon):
    code = await _code(client)
    for bad in ("_pytest_x", "Admin", "a", "system-thing", "has space"):
        r = await anon.post("/enrol", json={"code": code["code"], "name": bad})
        assert r.status_code == 400, bad
    r = await client.post("/rest/v1/principals", json={
        "name": "pytest-enrol-taken", "kind": "agent", "parent": TEST_ACCOUNT, "max_tier": 1,
        "repo_scope": []})
    assert r.status_code == 201, r.text
    r = await anon.post("/enrol", json={"code": code["code"], "name": "pytest-enrol-taken"})
    assert r.status_code == 409 and r.json()["code"] == "ENROL_NAME_TAKEN"
