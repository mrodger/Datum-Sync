"""Approval-gated calls, the review queue and the activity rollup (WP6).

spec/agent-auth-plane/05 §5, 07 §5, 08 §4. Same mock upstream as
test_federation.py; the ssh profile's restart_service carries the
`restart` approval label and the compute block lists it.
"""
from __future__ import annotations

import json

import httpx
import pytest
import pytest_asyncio

from datum_sync import auth, connections as conn_mod, crypto, db as db_module, lifecycle, pending, tokens
from datum_sync.api import app
from datum_sync.federation import catalogue, client as fedclient
from tests import mock_mcp_server as mock

pytestmark = pytest.mark.asyncio

ADMIN, SPONSOR, AGENT, OTHER = "_pytest_pc_admin", "_pytest_pc_sponsor", "_pytest_pc_agent", "_pytest_pc_other"
VM = "_pytest_pc_vm"
NAMES = (ADMIN, SPONSOR, AGENT, OTHER)


def _profile(name):
    from datum_sync.federation.routes import profiles

    return next(p["config"] for p in profiles() if p["id"] == name)


BLOCK = {"compute": {"connections": [VM], "hosts": ["vm102", "vm103"], "write": True,
                     "commands": {"allow": ["^ls\\b"], "deny": []},
                     "paths": {"read": [], "write": []}, "approval_required": ["restart"],
                     "tools": {"allow": ["*"], "deny": []}}}


@pytest_asyncio.fixture
async def pc(db, monkeypatch):
    if not crypto.available():
        crypto.set_test_keys(monkeypatch)
    monkeypatch.setattr(fedclient, "TRANSPORT", httpx.ASGITransport(app=mock.app))
    monkeypatch.setattr(mock, "DOWN", False)
    mock.CALLS.clear()
    await db.execute("DELETE FROM service_accounts WHERE name = ANY($1::text[])", list(NAMES))
    await db.execute("DELETE FROM connections WHERE name = $1", VM)
    admin_id = await db.fetchval(
        "INSERT INTO service_accounts (name, max_tier, repo_scope, federation_scope) VALUES ($1, 4, ARRAY['*'], $2) RETURNING id",
        ADMIN, json.dumps(BLOCK))
    sponsor_id = await db.fetchval(
        "INSERT INTO service_accounts (name, max_tier, repo_scope, federation_scope) VALUES ($1, 3, ARRAY['*'], $2) RETURNING id",
        SPONSOR, json.dumps(BLOCK))
    other_id = await db.fetchval(
        "INSERT INTO service_accounts (name, max_tier, repo_scope, federation_scope) VALUES ($1, 3, ARRAY['*'], $2) RETURNING id",
        OTHER, json.dumps(BLOCK))
    agent_id = await db.fetchval(
        "INSERT INTO service_accounts (name, kind, parent_id, max_tier, repo_scope, federation_scope) "
        "VALUES ($1, 'agent', $2, 3, ARRAY['*'], $3) RETURNING id", AGENT, sponsor_id, json.dumps(BLOCK))
    toks = {}
    for key, aid in (("admin", admin_id), ("sponsor", sponsor_id), ("other", other_id), ("agent", agent_id)):
        _, toks[key] = await tokens.create(db, aid, "t")
    cfg = dict(_profile("ssh-mcp"), url="http://mock.upstream/ssh/mcp", allow_private_origin=True,
               auth_inject={"type": "bearer", "secret_field": "token"})
    await conn_mod.create(db, name=VM, type_="mcp", config=cfg, secret={"token": "vm-secret-123456"}, tier=2,
                          caller_tier=5)
    await catalogue.refresh(db, await conn_mod.get(db, VM))
    await db_module.init_pool()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as c:
        yield {"client": c, "db": db, "agent_id": agent_id, "sponsor_id": sponsor_id, **toks}
    await db_module.close_pool()
    await db.execute("DELETE FROM pending_calls WHERE requested_by = $1", agent_id)
    await db.execute("DELETE FROM connections WHERE name = $1", VM)
    await db.execute("DELETE FROM service_accounts WHERE name = ANY($1::text[])", list(NAMES))


def _rpc(method, params=None, id_=1):
    return {"jsonrpc": "2.0", "id": id_, "method": method, "params": params or {}}


def bearer(raw):
    return {"authorization": f"Bearer {raw}"}


async def _session(c, raw):
    r = await c.post("/mcp", json=_rpc("initialize", {
        "protocolVersion": "2025-06-18", "clientInfo": {"name": "t", "version": "1"}, "capabilities": {}}),
        headers=bearer(raw))
    return {**bearer(raw), "Mcp-Session-Id": r.headers["Mcp-Session-Id"]}


async def _call(c, h, name, args):
    r = await c.post("/mcp", json=_rpc("tools/call", {"name": name, "arguments": args}), headers=h)
    return r.json()["result"]


async def _hold(pc, host="vm102"):
    c = pc["client"]
    h = await _session(c, pc["agent"])
    mock.CALLS.clear()
    out = await _call(c, h, "vm__restart_service", {"host": host, "service": "nginx"})
    assert out["isError"] is False and out["structuredContent"]["status"] == "pending_approval", out
    assert mock.CALLS == []
    return h, out["structuredContent"]["pending_id"]


async def test_a_held_call_is_not_forwarded_until_approved_and_then_once(pc):
    """FED-009.

    Guard: FED-009.
    """
    c, db = pc["client"], pc["db"]
    h, pid = await _hold(pc)
    out = await _call(c, h, "pending_status", {"pending_id": pid})
    assert out["structuredContent"]["status"] == "pending"
    row = await db.fetchrow("SELECT status, args, grant_snapshot FROM pending_calls WHERE id = $1", pid)
    assert row["status"] == "pending" and json.loads(row["args"])["service"] == "nginx"
    assert json.loads(row["grant_snapshot"])["effective_tier"] == 3

    r = await c.post(f"/rest/v1/pending-calls/{pid}/reject", headers=bearer(pc["sponsor"]), json={"reason": "not now"})
    assert r.status_code == 200 and r.json()["status"] == "rejected", r.text
    r = await c.post(f"/rest/v1/pending-calls/{pid}/approve", headers=bearer(pc["admin"]))
    assert r.status_code == 409, r.text
    assert mock.CALLS == [], "a rejected call is never forwarded"
    out = await _call(c, h, "pending_result", {"pending_id": pid})
    assert out["isError"] and out["structuredContent"]["code"] == "access_denied" and "not now" in out["content"][0]["text"]


async def test_an_approved_call_runs_under_the_requesters_snapshot(pc):
    """FED-010: narrowed after asking, the call still runs as approved.

    Guard: FED-010.
    """
    c, db = pc["client"], pc["db"]
    h, pid = await _hold(pc, host="vm103")
    narrowed = {"compute": dict(BLOCK["compute"], hosts=["vm102"])}
    await db.execute("UPDATE service_accounts SET federation_scope = $2 WHERE name = $1", AGENT, json.dumps(narrowed))
    r = await c.post(f"/rest/v1/pending-calls/{pid}/approve", headers=bearer(pc["sponsor"]))
    assert r.status_code == 200 and r.json()["status"] == "executed", r.text
    assert len(mock.CALLS) == 1 and mock.CALLS[0]["tool"] == "restart_service"
    assert mock.CALLS[0]["arguments"]["host"] == "vm103"
    r = await c.post(f"/rest/v1/pending-calls/{pid}/approve", headers=bearer(pc["sponsor"]))
    assert r.status_code == 409 and len(mock.CALLS) == 1
    # The requester fetches the result; nobody else can.
    out = await _call(c, h, "pending_result", {"pending_id": pid})
    assert out["isError"] is False and "restart_service" in out["content"][0]["text"]
    hs = await _session(c, pc["sponsor"])
    out = await _call(c, hs, "pending_result", {"pending_id": pid})
    assert out["isError"] and "NOT_FOUND" in out["content"][0]["text"]
    row = await db.fetchrow("SELECT verb FROM audit_log WHERE verb IN ('federate.approve', 'federate.execute') "
                            "AND detail->>'pending_id' = $1 ORDER BY id DESC LIMIT 1", str(pid))
    assert row["verb"] == "federate.execute"


async def test_deciding_needs_tier_4_or_the_requesters_sponsor(pc):
    """REV-002.

    Guard: REV-002.
    """
    c = pc["client"]
    _, pid = await _hold(pc)
    r = await c.post(f"/rest/v1/pending-calls/{pid}/approve", headers=bearer(pc["other"]))
    assert r.status_code == 403, r.text
    r = await c.get("/rest/v1/pending-calls", headers=bearer(pc["other"]))
    assert r.status_code == 200 and not [x for x in r.json()["items"] if x["id"] == pid]
    r = await c.get("/rest/v1/pending-calls", headers=bearer(pc["sponsor"]))
    assert [x for x in r.json()["items"] if x["id"] == pid]
    assert mock.CALLS == []
    r = await c.post(f"/rest/v1/pending-calls/{pid}/approve", headers=bearer(pc["sponsor"]))
    assert r.status_code == 200 and len(mock.CALLS) == 1


async def test_an_expired_call_cannot_be_approved(pc):
    """REV-003: the tick expires it; approval is refused; the agent asks again.

    Guard: REV-003.
    """
    c, db = pc["client"], pc["db"]
    h, pid = await _hold(pc)
    await db.execute("UPDATE pending_calls SET expires_at = now() - interval '1 minute' WHERE id = $1", pid)
    r = await c.post(f"/rest/v1/pending-calls/{pid}/approve", headers=bearer(pc["admin"]))
    assert r.status_code == 409 and r.json()["code"] == "EXPIRED", r.text
    done = await lifecycle.daily(db)
    assert done["expired_calls"] >= 1
    assert await db.fetchval("SELECT status FROM pending_calls WHERE id = $1", pid) == "expired"
    r = await c.post(f"/rest/v1/pending-calls/{pid}/approve", headers=bearer(pc["admin"]))
    assert r.status_code == 409
    assert mock.CALLS == []
    out = await _call(c, h, "pending_result", {"pending_id": pid})
    assert out["isError"] and out["structuredContent"]["code"] == "access_denied"


async def test_the_review_queue_lists_every_kind_with_a_link(pc):
    c, db = pc["client"], pc["db"]
    _, pid = await _hold(pc)
    await db.execute("INSERT INTO service_accounts (name, kind, parent_id, max_tier, repo_scope, state) "
                     "VALUES ('_pytest_pc_enrol', 'agent', $1, 2, ARRAY['*'], 'pending')", pc["sponsor_id"])
    await db.execute("UPDATE service_accounts SET review_due_at = now() - interval '1 day' WHERE name = $1", AGENT)
    await db.execute(
        "INSERT INTO oauth_clients (client_id, client_name, redirect_uris, grant_types) VALUES ('_pytest_pc_client', 'x', "
        "ARRAY['http://127.0.0.1/cb'], ARRAY['authorization_code']) ON CONFLICT (client_id) DO NOTHING")
    await db.execute(
        "INSERT INTO oauth_device_codes (device_code_hash, user_code, client_id, principal_id, scope, resource, "
        "interval_seconds, expires_at) VALUES ($1, 'PCQQ-TEST', '_pytest_pc_client', $2, 'mcp:operate', 'r', 5, "
        "now() + interval '1 hour')", auth.hash_token(auth.new_token()), pc["agent_id"])
    await db.execute("UPDATE connections SET federation_status = $2 WHERE name = $1", VM,
                     json.dumps({"last_ok_at": "2020-01-01T00:00:00+00:00", "tool_count": 4,
                                 "clashes": [{"upstream": "x", "tool_name": "vm__x"}]}))
    try:
        r = await c.get("/rest/v1/review", headers=bearer(pc["admin"]))
        assert r.status_code == 200, r.text
        body = r.json()
        kinds = {x["kind"]: x for x in body["items"]}
        assert {"enrolment", "elevation", "call", "review", "upstream", "clash"} <= set(kinds), body["counts"]
        assert kinds["elevation"]["link"] == "#/approvals?code=PCQQ-TEST"
        assert kinds["call"]["link"] == "#/approvals?tab=calls" and kinds["call"]["principal"] == AGENT
        assert kinds["review"]["link"] == f"#/admin/{AGENT}"
        assert kinds["enrolment"]["link"] == "#/enrolment?tab=pending"
        assert kinds["upstream"]["link"] == "#/mcp"
        # A sponsor sees its subtree only, and never the upstream rows.
        r = await c.get("/rest/v1/review", headers=bearer(pc["sponsor"]))
        skinds = {x["kind"] for x in r.json()["items"]}
        assert "call" in skinds and "upstream" not in skinds
        r = await c.get("/rest/v1/review", headers=bearer(pc["other"]))
        assert not [x for x in r.json()["items"] if x.get("principal") == AGENT]
        r = await c.get("/health")
        assert r.json()["pending_approvals"] >= 2 and "sessions_live" in r.json()
    finally:
        await db.execute("DELETE FROM service_accounts WHERE name = '_pytest_pc_enrol'")
        await db.execute("DELETE FROM oauth_clients WHERE client_id = '_pytest_pc_client'")


async def test_activity_is_visible_to_self_sponsor_and_tier_4_only(pc):
    """REV-001.

    Guard: REV-001.
    """
    c = pc["client"]
    await _hold(pc)
    for who, status in (("agent", 200), ("sponsor", 200), ("admin", 200), ("other", 404)):
        r = await c.get(f"/rest/v1/principals/{AGENT}/activity?window=7d", headers=bearer(pc[who]))
        assert r.status_code == status, (who, r.text)
    body = (await c.get(f"/rest/v1/principals/{AGENT}/activity?window=7d", headers=bearer(pc["admin"]))).json()
    assert body["total"] >= 2 and any(s["via"] == "mcp" for s in body["by_surface"])
    assert "top_targets" in body and body["last_seen"]
    sponsor = (await c.get(f"/rest/v1/principals/{AGENT}/activity?window=30d", headers=bearer(pc["sponsor"]))).json()
    assert "top_targets" not in sponsor and sponsor["window_days"] == 30
    r = await c.get(f"/rest/v1/principals/{AGENT}/activity?window=1y", headers=bearer(pc["admin"]))
    assert r.status_code == 400
