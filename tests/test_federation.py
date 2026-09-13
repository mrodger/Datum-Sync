"""Federation: the guard grammar, the cached catalogue, and calls through the
gateway to a mock upstream (spec/agent-auth-plane/05).

The upstream is `tests/mock_mcp_server.py` mounted through an ASGI
transport, so every request that reaches it is counted: "denied before
forwarding" is `CALLS == []`, not an inference.
"""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest
import pytest_asyncio

from datum_sync import audit, auth, config, connections as conn_mod, crypto, db as db_module, lifecycle, tokens
from datum_sync.api import app
from datum_sync.federation import catalogue, client as fedclient, guards
from tests import mock_mcp_server as mock

pytestmark = pytest.mark.asyncio

ACCOUNT = "_pytest_fed"
AGENT = "_pytest_fed_agent"
GH, VM, DRIVE = "_pytest_fed_gh", "_pytest_fed_vm", "_pytest_fed_drive"
TOKEN = "upstream-secret-token-123456"


def _profile(name):
    from datum_sync.federation.routes import profiles

    return next(p["config"] for p in profiles() if p["id"] == name)


@pytest_asyncio.fixture
async def fed(db, monkeypatch):
    if not crypto.available():
        crypto.set_test_keys(monkeypatch)
    monkeypatch.setattr(fedclient, "TRANSPORT", httpx.ASGITransport(app=mock.app))
    monkeypatch.setattr(mock, "DOWN", False)
    mock.CALLS.clear()
    await db.execute("DELETE FROM service_accounts WHERE name IN ($1, $2)", ACCOUNT, AGENT)
    await db.execute("DELETE FROM connections WHERE name = ANY($1::text[])", [GH, VM, DRIVE])
    scope = {
        "code": {"connections": [GH], "repos": ["datum/*"], "write": True, "paths": {"write": ["src/**"]},
                 "tools": {"allow": ["*"], "deny": ["*delete*", "list_commits"]}},
        "compute": {"connections": [VM], "hosts": ["vm102"],
                    "commands": {"allow": ["^(ls|cat|tail)\\b"], "deny": ["\\bsudo\\b", "\\brm\\b"]},
                    "paths": {"read": ["/home/ubuntu/**"], "write": []},
                    "approval_required": ["restart"], "tools": {"allow": ["*"], "deny": []}},
        "documents": {"connections": [DRIVE], "folders": ["folder-granted"], "write": False, "share": False,
                      "tools": {"allow": ["*"], "deny": []}},
    }
    account_id = await db.fetchval(
        "INSERT INTO service_accounts (name, max_tier, repo_scope, federation_scope) "
        "VALUES ($1, 4, ARRAY['*'], $2) RETURNING id", ACCOUNT, json.dumps(scope))
    agent_id = await db.fetchval(
        "INSERT INTO service_accounts (name, kind, parent_id, max_tier, repo_scope, federation_scope) "
        "VALUES ($1, 'agent', $2, 3, ARRAY['*'], $3) RETURNING id", AGENT, account_id, json.dumps(scope))
    _, admin = await tokens.create(db, account_id, "t")
    _, agent = await tokens.create(db, agent_id, "t")
    for name, profile, flavour, kind in ((GH, "github-mcp", "github", "code"), (VM, "ssh-mcp", "ssh", "compute"),
                                         (DRIVE, "gdrive-mcp", "drive", "documents")):
        cfg = dict(_profile(profile))
        cfg.update({"url": f"http://mock.upstream/{flavour}/mcp", "allow_private_origin": True,
                    "auth_inject": {"type": "bearer", "secret_field": "token"}})
        await conn_mod.create(db, name=name, type_="mcp", config=cfg, secret={"token": TOKEN}, tier=2,
                              caller_tier=5)
    await db_module.init_pool()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as c:
        # The catalogue as the worker tick would build it.
        for name in (GH, VM, DRIVE):
            await catalogue.refresh(db, await conn_mod.get(db, name))
        yield {"client": c, "db": db, "admin": admin, "agent": agent, "agent_id": agent_id,
               "account_id": account_id, "scope": scope}
    await db_module.close_pool()
    await db.execute("DELETE FROM connections WHERE name = ANY($1::text[])", [GH, VM, DRIVE])
    await db.execute("DELETE FROM service_accounts WHERE name IN ($1, $2)", ACCOUNT, AGENT)


def _rpc(method, params=None, id_=1):
    return {"jsonrpc": "2.0", "id": id_, "method": method, "params": params or {}}


async def _session(c, raw):
    r = await c.post("/mcp", json=_rpc("initialize", {
        "protocolVersion": "2025-06-18", "clientInfo": {"name": "t", "version": "1"}, "capabilities": {}}),
        headers={"authorization": f"Bearer {raw}"})
    return {"authorization": f"Bearer {raw}", "Mcp-Session-Id": r.headers["Mcp-Session-Id"]}


async def _tools(c, h):
    r = await c.post("/mcp", json=_rpc("tools/list"), headers=h)
    return {t["name"]: t for t in r.json()["result"]["tools"]}


async def _call(c, h, name, args):
    r = await c.post("/mcp", json=_rpc("tools/call", {"name": name, "arguments": args}), headers=h)
    return r.json()


async def _set_scope(db, scope):
    await db.execute("UPDATE service_accounts SET federation_scope = $2 WHERE name = ANY($1::text[])",
                     [ACCOUNT, AGENT], json.dumps(scope))


# -- the grammar, pure --------------------------------------------------------------


async def test_the_grammar_refuses_what_it_cannot_evaluate():
    with pytest.raises(guards.GuardError):
        guards.compile_guards([{"tools": ["x"], "checks": [{"grant": "nope", "value": {"path": "$.a"}}]}])
    with pytest.raises(guards.GuardError):
        guards.compile_guards([{"tools": ["x"], "checks": [{"grant": "repos", "value": {"path": "a.b"}}]}])
    with pytest.raises(guards.GuardError):
        guards.compile_guards([{"tools": ["x"], "requires": {"root": True}}])
    assert guards.parse_path("$.files[*].path") == ["files", "*", "path"]
    assert guards.parse_path("$[0].a") == [0, "a"]


async def test_commands_are_denied_before_allowed():
    """FED-008.

    Guard: FED-008.
    """
    deny, allow = guards.compile_commands({"commands": {"allow": ["^ls\\b", "^sudo ls\\b"], "deny": ["\\bsudo\\b"]}})
    assert guards.command_allowed("ls -la", deny, allow)
    assert not guards.command_allowed("sudo ls", deny, allow)
    assert not guards.command_allowed("rm -rf /", deny, allow)


async def test_join_denies_a_non_scalar_path():
    """FED-017.

    Guard: FED-017.
    """
    gs = guards.compile_guards([{"tools": ["t"], "checks": [{"grant": "repos", "value": {"join": ["$.owner", "$.repo"]}}]}])
    block = {"repos": ["datum/*"]}
    assert (await guards.evaluate(gs, block, 3, {"owner": "datum", "repo": "x"}))[0] == {"repos": ["datum/x"]}
    with pytest.raises(guards.Denied):
        await guards.evaluate(gs, block, 3, {"owner": ["datum"], "repo": "x"})
    with pytest.raises(guards.Denied):
        await guards.evaluate(gs, block, 3, {"owner": {"login": "datum"}, "repo": "x"})


async def test_a_missing_guarded_argument_is_a_deny():
    """FED-005.

    Guard: FED-005.
    """
    gs = guards.compile_guards([{"tools": ["t"], "checks": [{"grant": "hosts", "value": {"path": "$.host"}}]}])
    with pytest.raises(guards.Denied) as exc:
        await guards.evaluate(gs, {"hosts": ["*"]}, 3, {"command": "ls"})
    assert exc.value.field == "hosts"
    opt = guards.compile_guards([{"tools": ["t"], "checks": [{"grant": "hosts", "value": {"path": "$.host"}, "optional": True}]}])
    assert (await guards.evaluate(opt, {"hosts": ["*"]}, 3, {"command": "ls"}))[0] == {}


async def test_requires_tier_reads_the_effective_tier():
    """FED-007.

    Guard: FED-007.
    """
    gs = guards.compile_guards([{"tools": ["t"], "requires": {"tier": 5}}])
    with pytest.raises(guards.Denied):
        await guards.evaluate(gs, {}, 4, {})
    assert await guards.evaluate(gs, {}, 5, {}) == ({}, [])
    assert not guards.visible(gs, {}, 4) and guards.visible(gs, {}, 5)


async def test_schema_warnings_name_the_argument_a_tool_lacks():
    gs = guards.compile_guards([{"tools": ["get_*"], "checks": [{"grant": "repos", "value": {"path": "$.repository"}}]},
                               {"tools": ["nothing_*"]}])
    warn = guards.schema_warnings(gs, {"get_file": {"type": "object", "properties": {"owner": {}, "repo": {}}}})
    assert any("declares no argument 'repository'" in w for w in warn)
    assert any("matches no tool" in w for w in warn)


# -- the catalogue ---------------------------------------------------------------------


async def test_tools_list_is_the_block_filtered_cache(fed):
    """FED-001: only connections the block names; FED-002: the block's
    tools allow/deny; FED-003: unguarded tools hidden under default deny.

    Guard: FED-001, FED-002, FED-003.
    """
    c, db = fed["client"], fed["db"]
    h = await _session(c, fed["agent"])
    names = set(await _tools(c, h))
    assert {"gh__get_file_contents", "gh__push_files", "vm__run_command", "drive__get_file"} <= names
    # The block denies *delete* and list_commits (FED-002). list_commits is
    # the one that proves the filter: delete_repository is also hidden by
    # its guard's tier-5 requirement. get_me is guarded with an empty check
    # list, so it is listed.
    assert "gh__delete_repository" not in names and "gh__list_commits" not in names
    assert "gh__get_me" in names
    # An upstream tool no guard names is hidden under default deny (FED-003).
    await db.execute(
        "INSERT INTO federated_tools (connection, upstream_name, tool_name, input_schema) "
        "VALUES ($1, 'unguarded_thing', 'gh__unguarded_thing', '{}')", GH)
    assert "gh__unguarded_thing" not in set(await _tools(c, h))
    # A connection outside the block is not listed (FED-001), even at tier.
    scope = dict(fed["scope"])
    scope["compute"] = dict(scope["compute"], connections=[])
    await _set_scope(db, scope)
    names = set(await _tools(c, h))
    assert not any(n.startswith("vm__") for n in names) and "gh__get_file_contents" in names
    mock.CALLS.clear()
    assert mock.CALLS == [], "tools/list never touches an upstream"


async def test_a_tool_under_the_connections_tier_is_not_listed(fed):
    c, db = fed["client"], fed["db"]
    await db.execute("UPDATE connections SET tier = 4 WHERE name = $1", GH)
    h = await _session(c, fed["agent"])
    assert not any(n.startswith("gh__") for n in await _tools(c, h))
    h = await _session(c, fed["admin"])
    assert "gh__get_file_contents" in await _tools(c, h)


async def test_a_clash_with_a_workspace_tool_drops_the_upstream_tool(fed, workspace):
    """FED-019: workspace tools win, the upstream tool is dropped and audited.

    Guard: FED-019.
    """
    from datum_sync import mcp

    db = fed["db"]
    repo, ws = workspace
    local = mcp.tool_name(repo, ws)
    row = await conn_mod.get(db, GH)
    cfg = json.loads(row["config"])
    # Make the upstream's `get_me` land on the workspace tool's name.
    cfg["tool_prefix"] = local.split("__")[0]
    mock.TOOLS["github"].append({"name": local.split("__", 1)[1], "description": "clash",
                                 "inputSchema": {"type": "object", "properties": {}}})
    try:
        await conn_mod.update(db, GH, {"config": cfg}, caller_tier=5)
        status = await catalogue.refresh(db, await conn_mod.get(db, GH))
    finally:
        mock.TOOLS["github"].pop()
    assert status["clashes"] and status["clashes"][0]["tool_name"] == local
    assert await db.fetchval("SELECT count(*) FROM federated_tools WHERE tool_name = $1", local) == 0
    assert await db.fetchval(
        "SELECT count(*) FROM audit_log WHERE verb = 'federate.name_clash' AND target = $1", GH) >= 1


async def test_tool_names_are_capped_and_stable(fed):
    long = "a_really_long_upstream_tool_name_that_goes_on_and_on_and_on"
    a, b = catalogue.tool_name("gh", long), catalogue.tool_name("gh", long)
    assert a == b and len(a) <= 48 and a.startswith("gh__a_really")


# -- calls -----------------------------------------------------------------------------


async def test_a_value_outside_the_grant_is_denied_before_any_upstream_request(fed):
    """FED-004 and FED-013.

    Guard: FED-004, FED-013.
    """
    c, db = fed["client"], fed["db"]
    h = await _session(c, fed["agent"])
    mock.CALLS.clear()
    out = await _call(c, h, "gh__push_files", {"owner": "other", "repo": "repo", "files": [{"path": "a", "content": "x"}]})
    result = out["result"]
    assert result["isError"] and result["structuredContent"]["code"] == "FEDERATION_DENIED", out
    assert result["structuredContent"]["field"] == "repos"
    assert mock.CALLS == [], "denied before forwarding"

    out = await _call(c, h, "gh__push_files", {"owner": "datum", "repo": "gateway",
                                               "files": [{"path": "src/a.py", "content": "print(1)"}]})
    assert out["result"]["isError"] is False, out
    assert len(mock.CALLS) == 1 and mock.CALLS[0]["tool"] == "push_files"
    # Only the guarded values reach the audit row, never the file body (FED-013).
    row = await db.fetchrow(
        "SELECT detail FROM audit_log WHERE verb = 'federate.call' AND target = $1 ORDER BY id DESC LIMIT 1",
        f"{GH}:push_files")
    detail = json.loads(row["detail"])
    detail.pop("account", None)  # audit.write names the agent's sponsor
    assert detail == {"repos": ["datum/gateway"], "paths.write": ["src/a.py"]}
    assert "print(1)" not in row["detail"]
    denied = await db.fetchrow(
        "SELECT outcome, detail FROM audit_log WHERE verb = 'federate.denied' AND target = $1 ORDER BY id DESC LIMIT 1",
        f"{GH}:push_files")
    assert denied["outcome"] == "denied" and "other" not in denied["detail"]


async def test_requires_write_is_enforced(fed):
    """FED-006.

    Guard: FED-006.
    """
    c, db = fed["client"], fed["db"]
    scope = dict(fed["scope"])
    scope["code"] = dict(scope["code"], write=False)
    await _set_scope(db, scope)
    h = await _session(c, fed["agent"])
    assert "gh__push_files" not in await _tools(c, h)
    mock.CALLS.clear()
    out = await _call(c, h, "gh__push_files", {"owner": "datum", "repo": "gateway", "files": []})
    assert out["result"]["isError"] and "read-only" in out["result"]["content"][0]["text"]
    assert mock.CALLS == []
    out = await _call(c, h, "gh__get_file_contents", {"owner": "datum", "repo": "gateway", "path": "README"})
    assert out["result"]["isError"] is False


async def test_commands_and_hosts_on_the_compute_block(fed):
    c = fed["client"]
    h = await _session(c, fed["agent"])
    mock.CALLS.clear()
    out = await _call(c, h, "vm__run_command", {"host": "vm102", "command": "sudo ls"})
    assert out["result"]["isError"] and out["result"]["structuredContent"]["field"] == "commands"
    out = await _call(c, h, "vm__run_command", {"host": "vm102", "command": "ls -la"})
    assert out["result"]["isError"] is False
    out = await _call(c, h, "vm__run_command", {"command": "ls"})
    assert out["result"]["isError"] and out["result"]["structuredContent"]["field"] == "hosts"
    out = await _call(c, h, "vm__run_command", {"host": "vm999", "command": "ls"})
    assert out["result"]["isError"]
    assert [x["tool"] for x in mock.CALLS] == ["run_command"]


async def test_an_approval_gated_tool_is_held(fed):
    c = fed["client"]
    scope = dict(fed["scope"])
    scope["compute"] = dict(scope["compute"], write=True)
    await _set_scope(fed["db"], scope)
    h = await _session(c, fed["agent"])
    mock.CALLS.clear()
    out = await _call(c, h, "vm__restart_service", {"host": "vm102", "service": "x"})
    assert out["result"]["structuredContent"]["code"] == "APPROVAL_REQUIRED"
    assert mock.CALLS == []


async def test_drive_files_resolve_to_their_folder_and_failures_deny(fed):
    """FED-014.

    Guard: FED-014.
    """
    c = fed["client"]
    h = await _session(c, fed["agent"])
    out = await _call(c, h, "drive__get_file", {"fileId": "file-nested"})
    assert out["result"]["isError"] is False, out
    out = await _call(c, h, "drive__get_file", {"fileId": "file-elsewhere"})
    assert out["result"]["isError"] and out["result"]["structuredContent"]["field"] == "folders"
    out = await _call(c, h, "drive__get_file", {"fileId": "file-orphan"})
    assert out["result"]["isError"] and "resolve" in out["result"]["content"][0]["text"]
    out = await _call(c, h, "drive__share_file", {"fileId": "file-in-granted", "with": "x"})
    assert out["result"]["isError"] and "sharing" in out["result"]["content"][0]["text"]


async def test_the_inbound_authorization_is_never_forwarded_and_the_secret_never_returned(fed):
    """FED-011.

    Guard: FED-011.
    """
    c = fed["client"]
    h = await _session(c, fed["agent"])
    mock.CALLS.clear()
    out = await _call(c, h, "gh__get_me", {})
    assert mock.CALLS[0]["authorization"] == f"Bearer {TOKEN}"
    assert h["authorization"] not in mock.CALLS[0]["authorization"]
    text = json.dumps(out["result"])
    assert TOKEN not in text and "[redacted]" in text


async def test_an_upstream_that_is_down_lists_from_cache_and_fails_the_call_fast(fed, monkeypatch):
    """FED-012.

    Guard: FED-012.
    """
    c, db = fed["client"], fed["db"]
    monkeypatch.setattr(mock, "DOWN", True)
    h = await _session(c, fed["agent"])
    t0 = asyncio.get_event_loop().time()
    names = await _tools(c, h)
    assert "gh__get_file_contents" in names
    assert asyncio.get_event_loop().time() - t0 < 5
    out = await _call(c, h, "gh__get_file_contents", {"owner": "datum", "repo": "gateway", "path": "x"})
    assert out["result"]["isError"] and out["result"]["structuredContent"]["code"] in ("UPSTREAM_UNAVAILABLE", "UPSTREAM_ERROR")
    status = await catalogue.refresh(db, await conn_mod.get(db, GH))
    assert status["last_error"]
    r = await c.get("/health")
    fh = next(x for x in r.json()["federation"] if x["name"] == GH)
    assert fh["status"] in ("stale", "down") and fh["last_error"]


async def test_arguments_over_the_cap_are_refused_before_evaluation(fed):
    """FED-020.

    Guard: FED-020.
    """
    c = fed["client"]
    h = await _session(c, fed["agent"])
    mock.CALLS.clear()
    big = {"owner": "datum", "repo": "gateway", "files": [{"path": "a", "content": "x" * (guards.MAX_ARGS_BYTES + 10)}]}
    out = await _call(c, h, "gh__push_files", big)
    assert out["result"]["structuredContent"]["code"] == "ARGUMENTS_TOO_LARGE"
    assert mock.CALLS == []


async def test_resources_are_listed_rewritten_and_read_under_a_guard(fed):
    """FED-016.

    Guard: FED-016.
    """
    c = fed["client"]
    h = await _session(c, fed["agent"])
    r = await c.post("/mcp", json=_rpc("resources/list"), headers=h)
    uris = {x["uri"] for x in r.json()["result"]["resources"]}
    assert f"datum://{GH}/repo://datum/gateway/README.md" in uris
    mock.CALLS.clear()
    r = await c.post("/mcp", json=_rpc("resources/read", {"uri": f"datum://{GH}/repo://other/repo/README.md"}), headers=h)
    assert "error" in r.json() and "denied" in r.json()["error"]["message"]
    assert mock.CALLS == []
    r = await c.post("/mcp", json=_rpc("resources/read", {"uri": f"datum://{GH}/repo://datum/gateway/README.md"}), headers=h)
    body = r.json()["result"]["contents"][0]
    assert body["text"] == "# hello" and body["uri"].startswith("datum://")


async def test_child_federation_blocks_must_narrow_the_parents(fed):
    """FED-015: the grant write refuses a wider child block.

    Guard: FED-015.
    """
    c = fed["client"]
    wider = dict(fed["scope"])
    wider["code"] = dict(wider["code"], repos=["*/*"])
    r = await c.patch(f"/rest/v1/principals/{AGENT}", headers={"authorization": f"Bearer {fed['admin']}"},
                      json={"federation_scope": wider})
    assert r.status_code == 400, r.text
    assert "federation_scope.code.repos" in r.text


async def test_private_origin_needs_tier_5_at_save(fed):
    """FED-018.

    Guard: FED-018.
    """
    c = fed["client"]
    cfg = dict(_profile("ssh-mcp"), url="http://10.0.0.5/mcp", allow_private_origin=True)
    r = await c.post("/rest/v1/connections", headers={"authorization": f"Bearer {fed['admin']}"},
                     json={"name": "_pytest_fed_lan", "type": "mcp", "config": cfg, "tier": 2})
    assert r.status_code == 400 and "tier 5" in r.json()["message"], r.text


async def test_the_federation_screen_data_names_why_a_principal_cannot_see_a_tool(fed):
    c = fed["client"]
    scope = dict(fed["scope"])
    scope["code"] = dict(scope["code"], write=False)
    await _set_scope(fed["db"], scope)
    r = await c.get(f"/rest/v1/federation?as={AGENT}", headers={"authorization": f"Bearer {fed['admin']}"})
    assert r.status_code == 200, r.text
    gh = next(x for x in r.json()["items"] if x["name"] == GH)
    assert gh["status"] == "ok" and gh["coverage"]["guarded"] >= 4
    push = next(t for t in gh["tools"] if t["upstream_name"] == "push_files")
    assert push["visible"] is False and "read-only" in push["why"]
    delete = next(t for t in gh["tools"] if t["upstream_name"] == "delete_repository")
    assert delete["visible"] is False and "allow/deny" in delete["why"]
    r = await c.get("/rest/v1/federation/profiles", headers={"authorization": f"Bearer {fed['admin']}"})
    assert {p["id"] for p in r.json()["items"]} >= {"github-mcp", "ssh-mcp", "gdrive-mcp"}
    r = await c.post(f"/rest/v1/federation/{GH}/refresh", headers={"authorization": f"Bearer {fed['admin']}"})
    assert r.status_code == 200 and r.json()["status"] == "ok"
