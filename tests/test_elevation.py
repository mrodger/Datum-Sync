"""OAuth elevation: scopes, the device flow, the consent page, hygiene.

spec/agent-auth-plane/04 and 12 §3. The wire is httpx against the app. The
consent page is driven the way a browser would post it; the device flow the
way a script (or the `elevate` built-in's caller) polls it.

Three principals: a tier-4 administrator, a tier-3 sponsor with one agent
child, and an unrelated tier-3 human who sponsors nothing. The agent holds a
baseline token capped at tier 2, so an elevation to `mcp:operate` is visible
as `effective_tier` moving from 2 to 3.
"""
from __future__ import annotations

import base64
import hashlib
import secrets
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import pytest_asyncio

from datum_sync import auth, config, db as db_module, device, lifecycle, mcp, oauth, tokens
from datum_sync.api import app

pytestmark = pytest.mark.asyncio

ADMIN = "_pytest_elev_admin"
SPONSOR = "_pytest_elev_sponsor"
AGENT = "_pytest_elev_agent"
OTHER = "_pytest_elev_other"
PASSWORD = "elevation-pass-1"
NAMES = (ADMIN, SPONSOR, AGENT, OTHER)
REDIRECT_URI = "http://127.0.0.1:9797/callback"
DEVICE_GRANT = device.DEVICE_GRANT


@pytest_asyncio.fixture
async def elev(db):
    await db.execute("DELETE FROM service_accounts WHERE name = ANY($1::text[])", list(NAMES))
    pw = auth.hash_password(PASSWORD)
    admin_id = await db.fetchval(
        "INSERT INTO service_accounts (name, max_tier, repo_scope, password_hash) "
        "VALUES ($1, 4, ARRAY['*'], $2) RETURNING id", ADMIN, pw)
    sponsor_id = await db.fetchval(
        "INSERT INTO service_accounts (name, max_tier, repo_scope, password_hash) "
        "VALUES ($1, 3, ARRAY['*'], $2) RETURNING id", SPONSOR, pw)
    other_id = await db.fetchval(
        "INSERT INTO service_accounts (name, max_tier, repo_scope, password_hash) "
        "VALUES ($1, 3, ARRAY['*'], $2) RETURNING id", OTHER, pw)
    agent_id = await db.fetchval(
        "INSERT INTO service_accounts (name, kind, parent_id, max_tier, repo_scope) "
        "VALUES ($1, 'agent', $2, 3, ARRAY['*']) RETURNING id", AGENT, sponsor_id)
    _, admin = await tokens.create(db, admin_id, "t")
    _, sponsor = await tokens.create(db, sponsor_id, "t")
    _, other = await tokens.create(db, other_id, "t")
    _, baseline = await tokens.create(db, agent_id, "baseline", max_tier=2)
    await db_module.init_pool()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as c:
        yield {"client": c, "db": db, "admin": admin, "sponsor": sponsor, "other": other,
               "baseline": baseline, "agent_id": agent_id, "sponsor_id": sponsor_id}
    await db_module.close_pool()
    await db.execute("DELETE FROM oauth_clients WHERE client_name LIKE '_pytest_elev%'")
    await db.execute("DELETE FROM service_accounts WHERE name = ANY($1::text[])", list(NAMES))


def bearer(raw):
    return {"authorization": f"Bearer {raw}"}


def pkce():
    verifier = secrets.token_urlsafe(64)[:96]
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return verifier, base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


async def register(client, name="_pytest_elev client", **extra):
    r = await client.post("/oauth/register", json={
        "redirect_uris": [REDIRECT_URI], "client_name": name, **extra})
    assert r.status_code == 201, r.text
    return r.json()["client_id"]


async def consent(client, client_id, challenge, username, scope="", on_behalf_of=""):
    return await client.post("/oauth/authorize", data={
        "username": username, "password": PASSWORD, "client_id": client_id,
        "redirect_uri": REDIRECT_URI, "code_challenge": challenge, "resource": auth.MCP_RESOURCE,
        "state": "s", "scope": scope, "on_behalf_of": on_behalf_of})


async def pkce_token(client, username, scope="", on_behalf_of="", client_id=None):
    """Register, consent, exchange: the whole login as a client does it."""
    client_id = client_id or await register(client)
    verifier, challenge = pkce()
    r = await consent(client, client_id, challenge, username, scope, on_behalf_of)
    assert r.status_code == 302, r.text
    code = parse_qs(urlparse(r.headers["location"]).query)["code"][0]
    r = await client.post("/oauth/token", data={
        "grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT_URI,
        "code_verifier": verifier, "client_id": client_id, "resource": auth.MCP_RESOURCE})
    assert r.status_code == 200, r.text
    return r.json(), client_id


async def whoami(client, raw):
    r = await client.get("/rest/v1/whoami", headers=bearer(raw))
    return r.status_code, r.json()


async def ask(client, raw, scope="mcp:operate"):
    r = await client.post("/oauth/device", headers=bearer(raw),
                          data={"client_id": device.ELEVATE_CLIENT_ID, "scope": scope})
    return r.status_code, r.json()


async def poll(client, device_code):
    r = await client.post("/oauth/token", data={
        "grant_type": DEVICE_GRANT, "client_id": device.ELEVATE_CLIENT_ID, "device_code": device_code})
    return r.status_code, r.json()


async def unthrottle(db, device_code):
    """Polling is rate-limited on the wire; the tests are not waiting 5 s."""
    await db.execute("UPDATE oauth_device_codes SET last_polled_at = NULL WHERE device_code_hash = $1",
                     auth.hash_token(device_code))


async def approve(client, raw, elevation_id, scope=None):
    body = {"scope": scope} if scope else {}
    return await client.post(f"/rest/v1/elevations/{elevation_id}/approve", headers=bearer(raw), json=body)


async def pending_id(client, raw):
    r = await client.get("/rest/v1/elevations", headers=bearer(raw))
    assert r.status_code == 200, r.text
    items = [x for x in r.json()["items"] if x["principal"] == AGENT]
    assert items, r.text
    return items[0]["id"]


# -- scopes at authorize ------------------------------------------------------------


async def test_an_unknown_scope_is_refused_at_authorize(elev):
    """ELEV-002: the vocabulary is checked where the client can be told.

    Guard: ELEV-002.
    """
    c = elev["client"]
    client_id = await register(c)
    _, challenge = pkce()
    r = await c.get("/oauth/authorize", params={
        "client_id": client_id, "redirect_uri": REDIRECT_URI, "response_type": "code",
        "code_challenge": challenge, "code_challenge_method": "S256", "scope": "mcp:root"})
    assert r.status_code == 302, r.text
    q = parse_qs(urlparse(r.headers["location"]).query)
    assert q["error"] == ["invalid_scope"], q


async def test_registration_without_scope_is_accepted_and_authorize_still_validates(elev):
    """ELEV-012: Codex registers with no scope and asks later (12 §3.4).

    Guard: ELEV-012.
    """
    c = elev["client"]
    r = await c.post("/oauth/register", json={"redirect_uris": [REDIRECT_URI],
                                               "client_name": "_pytest_elev Codex CLI 0.x"})
    assert r.status_code == 201, r.text
    client_id = r.json()["client_id"]
    _, challenge = pkce()
    r = await c.get("/oauth/authorize", params={
        "client_id": client_id, "redirect_uri": REDIRECT_URI, "response_type": "code",
        "code_challenge": challenge, "code_challenge_method": "S256", "scope": "nope"})
    assert parse_qs(urlparse(r.headers["location"]).query)["error"] == ["invalid_scope"]


async def test_the_consent_page_offers_the_picker_when_no_scope_was_asked(elev):
    c = elev["client"]
    client_id = await register(c)
    _, challenge = pkce()
    r = await c.get("/oauth/authorize", params={
        "client_id": client_id, "redirect_uri": REDIRECT_URI, "response_type": "code",
        "code_challenge": challenge, "code_challenge_method": "S256"})
    assert r.status_code == 200
    assert 'name=scope' in r.text and "mcp:operate" in r.text


async def test_the_chosen_scope_is_clamped_by_the_principals_tier(elev):
    """ELEV-011: the picker renders before sign-in, so the ceiling is applied
    after it. A tier-3 person choosing `mcp:admin` gets a tier-3 grant.

    Guard: ELEV-011.
    """
    c = elev["client"]
    body, _ = await pkce_token(c, SPONSOR, scope="mcp:admin")
    assert body.get("scope") != "mcp:admin", body
    assert auth.scope_tier(body.get("scope", "mcp")) <= 3
    _, me = await whoami(c, body["access_token"])
    assert me["name"] == SPONSOR and me["effective_tier"] <= 3


async def test_a_pinned_scope_flows_to_the_token(elev):
    """ELEV-001 is TIER-003: the scope caps the effective tier. Here the
    whole login carries it: `mcp` on a tier-4 administrator is tier 2."""
    c = elev["client"]
    body, _ = await pkce_token(c, ADMIN, scope="mcp")
    assert body["scope"] == "mcp"
    _, me = await whoami(c, body["access_token"])
    assert me["effective_tier"] == 2 and me["max_tier"] == 4
    assert "mcp:operate" in me["elevate"] and "mcp:admin" in me["elevate"]


# -- on_behalf_of ---------------------------------------------------------------------


async def test_on_behalf_of_needs_the_sponsor_or_tier_4(elev):
    """ELEV-004: an unrelated tier-3 human cannot bind a grant to someone
    else's agent; its sponsor can, and the token is the agent's.

    Guard: ELEV-004.
    """
    c = elev["client"]
    client_id = await register(c)
    _, challenge = pkce()
    r = await consent(c, client_id, challenge, OTHER, scope="mcp:operate", on_behalf_of=AGENT)
    assert r.status_code == 403, r.text

    body, _ = await pkce_token(c, SPONSOR, scope="mcp:operate", on_behalf_of=AGENT, client_id=client_id)
    _, me = await whoami(c, body["access_token"])
    assert me["name"] == AGENT and me["kind"] == "agent" and me["effective_tier"] == 3

    row = await elev["db"].fetchrow(
        "SELECT actor_name, detail FROM audit_log WHERE verb = 'oauth.consent' AND target = $1 "
        "ORDER BY id DESC LIMIT 1", AGENT)
    assert row["actor_name"] == SPONSOR
    assert '"on_behalf_of": "%s"' % AGENT in row["detail"]


async def test_on_behalf_of_is_capped_by_the_sponsors_own_tier(elev):
    c = elev["client"]
    await elev["db"].execute("UPDATE service_accounts SET max_tier = 2 WHERE name = $1", SPONSOR)
    body, _ = await pkce_token(c, SPONSOR, scope="mcp:operate", on_behalf_of=AGENT)
    _, me = await whoami(c, body["access_token"])
    assert me["name"] == AGENT and me["effective_tier"] == 2


# -- the device flow ------------------------------------------------------------------


async def test_the_device_request_needs_a_bearer(elev):
    """ELEV-005: the request is for *this* principal, so it carries its credential.

    Guard: ELEV-005.
    """
    c = elev["client"]
    r = await c.post("/oauth/device", data={"client_id": device.ELEVATE_CLIENT_ID, "scope": "mcp"})
    assert r.status_code == 401, r.text


async def test_the_device_flow_end_to_end(elev):
    """ELEV-007: nothing mints until a person has decided.

    Guard: ELEV-007.
    """
    c, db = elev["client"], elev["db"]
    _, me = await whoami(c, elev["baseline"])
    assert me["effective_tier"] == 2 and list(me["elevate"]) == ["mcp:operate"]

    status, asked = await ask(c, elev["baseline"])
    assert status == 200, asked
    assert asked["auto_approved"] is False and "-" in asked["user_code"]
    assert asked["verification_uri"].endswith("#/approvals")

    status, body = await poll(c, asked["device_code"])
    assert status == 400 and body["error"] == "authorization_pending", body

    eid = await pending_id(c, elev["sponsor"])
    r = await approve(c, elev["sponsor"], eid)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "approved" and r.json()["decided_by"] == SPONSOR

    await unthrottle(db, asked["device_code"])
    status, body = await poll(c, asked["device_code"])
    assert status == 200, body
    assert body["scope"] == "mcp:operate" and body["refresh_token"]
    _, me = await whoami(c, body["access_token"])
    assert me["name"] == AGENT and me["effective_tier"] == 3 and me["source"] == "oauth"
    # The baseline token is untouched: elevation is a second credential.
    _, me = await whoami(c, elev["baseline"])
    assert me["effective_tier"] == 2


async def test_a_device_code_is_single_use_and_reuse_revokes_the_family(elev):
    """ELEV-006: presenting a consumed device code is what a copied credential
    looks like, and every token of that client and principal dies with it.

    Guard: ELEV-006.
    """
    c, db = elev["client"], elev["db"]
    _, asked = await ask(c, elev["baseline"])
    await approve(c, elev["sponsor"], await pending_id(c, elev["sponsor"]))
    await unthrottle(db, asked["device_code"])
    status, first = await poll(c, asked["device_code"])
    assert status == 200, first
    assert (await whoami(c, first["access_token"]))[0] == 200

    await unthrottle(db, asked["device_code"])
    status, again = await poll(c, asked["device_code"])
    assert status == 400 and again["error"] == "invalid_grant", again
    assert (await whoami(c, first["access_token"]))[0] == 401


async def test_polling_faster_than_the_interval_is_slowed(elev):
    """ELEV-008.

    Guard: ELEV-008.
    """
    c = elev["client"]
    _, asked = await ask(c, elev["baseline"])
    status, body = await poll(c, asked["device_code"])
    assert body["error"] == "authorization_pending"
    status, body = await poll(c, asked["device_code"])
    assert status == 400 and body["error"] == "slow_down", body
    interval = await elev["db"].fetchval(
        "SELECT interval_seconds FROM oauth_device_codes WHERE device_code_hash = $1",
        auth.hash_token(asked["device_code"]))
    assert interval == config.DEVICE_POLL_INTERVAL_SECONDS + 5


async def test_the_baseline_scope_is_approved_by_policy(elev):
    c = elev["client"]
    status, asked = await ask(c, elev["baseline"], scope="mcp")
    assert asked["auto_approved"] is True
    status, body = await poll(c, asked["device_code"])
    assert status == 200 and body["scope"] == "mcp", body
    _, me = await whoami(c, body["access_token"])
    assert me["name"] == AGENT and me["effective_tier"] == 2


async def test_a_scope_above_the_principals_tier_is_refused_at_request(elev):
    c = elev["client"]
    status, body = await ask(c, elev["baseline"], scope="mcp:admin")
    assert status == 400 and body["error"] == "invalid_scope", body


# -- approvals ------------------------------------------------------------------------


async def test_an_approver_may_narrow_but_never_widen(elev):
    """ELEV-003.

    Guard: ELEV-003.
    """
    c, db = elev["client"], elev["db"]
    _, asked = await ask(c, elev["baseline"], scope="mcp mcp:operate")
    eid = await pending_id(c, elev["sponsor"])
    r = await approve(c, elev["sponsor"], eid, scope="mcp:admin")
    assert r.status_code == 400 and r.json()["code"] == "SCOPE_WIDER_THAN_REQUESTED", r.text
    r = await approve(c, elev["sponsor"], eid, scope="mcp")
    assert r.status_code == 200, r.text
    assert r.json()["approved_scope"] == "mcp"
    await unthrottle(db, asked["device_code"])
    status, body = await poll(c, asked["device_code"])
    assert status == 200 and body["scope"] == "mcp"
    _, me = await whoami(c, body["access_token"])
    assert me["effective_tier"] == 2


async def test_only_tier_4_or_the_sponsor_may_decide(elev):
    c = elev["client"]
    await ask(c, elev["baseline"])
    r = await c.get("/rest/v1/elevations", headers=bearer(elev["other"]))
    assert r.status_code == 200 and not [x for x in r.json()["items"] if x["principal"] == AGENT]
    eid = await pending_id(c, elev["admin"])
    r = await approve(c, elev["other"], eid)
    assert r.status_code == 403, r.text
    r = await c.post(f"/rest/v1/elevations/{eid}/deny", headers=bearer(elev["admin"]), json={"reason": "no"})
    assert r.status_code == 200 and r.json()["status"] == "denied"
    r = await approve(c, elev["sponsor"], eid)
    assert r.status_code == 409


async def test_a_denied_request_reports_access_denied(elev):
    c = elev["client"]
    _, asked = await ask(c, elev["baseline"])
    eid = await pending_id(c, elev["sponsor"])
    await c.post(f"/rest/v1/elevations/{eid}/deny", headers=bearer(elev["sponsor"]), json={"reason": "not yet"})
    status, body = await poll(c, asked["device_code"])
    assert status == 400 and body["error"] == "access_denied" and "not yet" in body["error_description"]


# -- refresh ---------------------------------------------------------------------------


async def test_a_refresh_cannot_widen_the_scope(elev):
    """ELEV-009.

    Guard: ELEV-009.
    """
    c = elev["client"]
    body, client_id = await pkce_token(c, ADMIN, scope="mcp")
    r = await c.post("/oauth/token", data={
        "grant_type": "refresh_token", "refresh_token": body["refresh_token"],
        "client_id": client_id, "scope": "mcp:admin"})
    assert r.status_code == 400 and r.json()["error"] == "invalid_scope", r.text
    r = await c.post("/oauth/token", data={
        "grant_type": "refresh_token", "refresh_token": body["refresh_token"], "client_id": client_id})
    assert r.status_code == 200 and r.json()["scope"] == "mcp", r.text
    _, me = await whoami(c, r.json()["access_token"])
    assert me["effective_tier"] == 2


# -- the MCP built-in -------------------------------------------------------------------


def _rpc(method, params=None, id_=1):
    return {"jsonrpc": "2.0", "id": id_, "method": method, "params": params or {}}


async def _session(c, raw):
    r = await c.post("/mcp", json=_rpc("initialize", {
        "protocolVersion": mcp.PROTOCOL_VERSION, "clientInfo": {"name": "t", "version": "1"},
        "capabilities": {}}), headers=bearer(raw))
    return {**bearer(raw), "Mcp-Session-Id": r.headers["Mcp-Session-Id"]}


async def test_the_elevate_tool_is_offered_only_when_something_unlocks(elev):
    c = elev["client"]
    h = await _session(c, elev["baseline"])
    names = [t["name"] for t in (await c.post("/mcp", json=_rpc("tools/list"), headers=h)).json()["result"]["tools"]]
    assert "elevate" in names
    r = await c.post("/mcp", json=_rpc("tools/call", {"name": "elevate", "arguments": {"scope": "mcp:operate"}}), headers=h)
    result = r.json()["result"]
    assert result["isError"] is False and "-" in result["structuredContent"]["user_code"]
    assert "Approvals" in result["content"][0]["text"] or "#/approvals" in result["content"][0]["text"]

    h = await _session(c, elev["admin"])
    names = [t["name"] for t in (await c.post("/mcp", json=_rpc("tools/list"), headers=h)).json()["result"]["tools"]]
    assert "elevate" not in names


# -- hygiene ---------------------------------------------------------------------------


async def test_registration_is_rate_limited(elev, monkeypatch):
    c = elev["client"]
    monkeypatch.setattr(config, "OAUTH_REGISTER_PER_HOUR", 3)
    oauth._registrations.clear()
    for _ in range(3):
        await register(c)
    r = await c.post("/oauth/register", json={"redirect_uris": [REDIRECT_URI], "client_name": "_pytest_elev x"})
    assert r.status_code == 429 and r.json()["error"] == "too_many_registrations", r.text


async def test_unused_clients_are_pruned_and_referenced_ones_kept(elev):
    """ELEV-010.

    Guard: ELEV-010.
    """
    c, db = elev["client"], elev["db"]
    idle = await register(c, name="_pytest_elev idle")
    used = await register(c, name="_pytest_elev used")
    fresh = await register(c, name="_pytest_elev fresh")
    await db.execute("UPDATE oauth_clients SET created_at = now() - interval '40 days' WHERE client_id = ANY($1::text[])",
                     [idle, used])
    await db.execute(
        "INSERT INTO oauth_device_codes (device_code_hash, user_code, client_id, principal_id, scope, "
        "resource, interval_seconds, expires_at) VALUES ($1, 'ABCD-EFGH', $2, $3, 'mcp', 'r', 5, now() + interval '1 hour')",
        auth.hash_token(auth.new_token()), used, elev["agent_id"])
    out = await lifecycle.daily(db)
    assert out["pruned_clients"] >= 1
    left = {r["client_id"] for r in await db.fetch(
        "SELECT client_id FROM oauth_clients WHERE client_id = ANY($1::text[])", [idle, used, fresh])}
    assert idle not in left and used in left and fresh in left, left
    # The list is the 100 newest; bring the survivor back into view.
    await db.execute("UPDATE oauth_clients SET created_at = now() WHERE client_id = $1", used)
    r = await c.get("/rest/v1/auth/clients", headers=bearer(elev["admin"]))
    body = r.json()
    assert body["pruned_last_run"] >= 1 and body["retention_days"] == config.RETENTION_UNUSED_OAUTH_CLIENT_DAYS
    row = next(x for x in body["items"] if x["client_id"] == used)
    assert row["device_requests"] == 1 and row["unused"] is False
    row = next(x for x in body["items"] if x["client_id"] == fresh)
    assert row["unused"] is True
