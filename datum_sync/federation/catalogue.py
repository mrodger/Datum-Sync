"""The cached catalogue of federated tools (spec/agent-auth-plane/05 §3).

`refresh` talks to an upstream; nothing else here does. `tools/list` for a
principal is a query over `federated_tools` filtered by the principal's
federation block, so an upstream that is down serves its last catalogue and
a call fails fast rather than a listing hanging.
"""
from __future__ import annotations

import json
import re
import time
from typing import Any

import asyncpg

from datum_sync import audit, config, connections, crypto
from datum_sync.auth import Principal
from datum_sync.federation import client as fedclient, guards as guards_mod

_UNSAFE = re.compile(r"[^A-Za-z0-9_.-]")


def tool_name(prefix: str, upstream_name: str) -> str:
    from datum_sync import mcp  # lazy: mcp imports this module

    return mcp.cap_tool_name(_UNSAFE.sub("_", f"{prefix}__{upstream_name}"))


def _status(row: asyncpg.Record) -> dict[str, Any]:
    raw = row["federation_status"]
    return json.loads(raw) if isinstance(raw, str) else dict(raw or {})


async def _write_status(conn: asyncpg.Connection, name: str, status: dict[str, Any]) -> None:
    await conn.execute(
        "UPDATE connections SET federation_status = $2 WHERE name = $1", name, json.dumps(status)
    )


async def upstream_for(conn: asyncpg.Connection, row: asyncpg.Record) -> fedclient.Upstream:
    cfg = json.loads(row["config"])
    blob = await connections._sealed(conn, row["name"])
    secret = crypto.open_(row["name"], blob) if blob else {}
    return fedclient.Upstream(row["name"], cfg, secret)


async def workspace_tool_names(conn: asyncpg.Connection) -> set[str]:
    from datum_sync import mcp

    rows = await conn.fetch(
        "SELECT r.name AS repository, w.name AS workspace FROM workspaces w "
        "JOIN repositories r ON r.id = w.repository_id"
    )
    return {mcp.tool_name(r["repository"], r["workspace"]) for r in rows}


async def refresh(
    conn: asyncpg.Connection, row: asyncpg.Record, trace: audit.Trace | None = None
) -> dict[str, Any]:
    """initialize + tools/list (+ resources) against one upstream; upsert the cache.

    Never raises: the outcome lands in `connections.federation_status` and
    an audit row, because the caller is a worker tick or a save.
    """
    name = row["name"]
    cfg = json.loads(row["config"])
    status = _status(row)
    trace = trace or audit.Trace.mint(None)
    try:
        upstream = await upstream_for(conn, row)
        await upstream.initialize()
        tools = await upstream.tools_list()
        resources = await upstream.resources_list()
        templates = await upstream.resources_list(templates=True)
    except (fedclient.UpstreamError, fedclient.ToolError, Exception) as exc:  # noqa: BLE001
        status.update({"last_error": f"{type(exc).__name__}: {exc}"[:500],
                       "last_attempt_at": _now_iso()})
        await _write_status(conn, name, status)
        await audit.write_anon(
            conn, trace=trace, via="worker", verb="federate.unavailable", target_kind="connection",
            target=name, outcome="error", detail={"error": status["last_error"]},
        )
        return status

    compiled = guards_mod.compile_guards(cfg.get("guards"))
    prefix = cfg.get("tool_prefix") or name
    taken = await workspace_tool_names(conn)
    others = await conn.fetch(
        "SELECT tool_name FROM federated_tools WHERE connection <> $1", name
    )
    taken |= {r["tool_name"] for r in others}
    clashes: list[dict[str, str]] = []
    kept: list[str] = []
    schemas: dict[str, dict[str, Any]] = {}
    try:
        async with conn.transaction():
            await conn.execute("DELETE FROM federated_tools WHERE connection = $1", name)
            for t in tools:
                upstream_name = str(t.get("name") or "")
                if not upstream_name:
                    continue
                gateway_name = tool_name(prefix, upstream_name)
                if gateway_name in taken:
                    # Workspace tools win (D-17); a second upstream claiming the
                    # same name loses to whoever refreshed first (FED-019).
                    clashes.append({"upstream": upstream_name, "tool_name": gateway_name})
                    continue
                schema = t.get("inputSchema") or {"type": "object"}
                schemas[upstream_name] = schema
                guarded = any(g.matches(upstream_name) for g in compiled)
                await conn.execute(
                    """
                    INSERT INTO federated_tools
                        (connection, upstream_name, tool_name, description, input_schema, annotations, guarded)
                    VALUES ($1, $2, $3, $4, $5, $6, $7)
                    """,
                    name, upstream_name, gateway_name, t.get("description"),
                    json.dumps(schema), json.dumps(t.get("annotations")) if t.get("annotations") else None,
                    guarded,
                )
                kept.append(upstream_name)
            await conn.execute("DELETE FROM federated_resources WHERE connection = $1", name)
            for r, is_template in [(r, False) for r in resources] + [(r, True) for r in templates]:
                uri = r.get("uriTemplate" if is_template else "uri")
                if not uri:
                    continue
                await conn.execute(
                    """
                    INSERT INTO federated_resources (connection, upstream_uri, name, description, mime_type, is_template)
                    VALUES ($1, $2, $3, $4, $5, $6) ON CONFLICT (connection, upstream_uri) DO NOTHING
                    """,
                    name, str(uri), r.get("name"), r.get("description"), r.get("mimeType"), is_template,
                )
    except asyncpg.PostgresError as exc:
        # The connection was deleted under us, or a row cannot be written:
        # recorded, not raised, like an upstream failure. A refresh is a tick.
        status.update({"last_error": f"{type(exc).__name__}: {exc}"[:500], "last_attempt_at": _now_iso()})
        try:
            await _write_status(conn, name, status)
        except asyncpg.PostgresError:
            pass
        return status
    warnings = guards_mod.schema_warnings(compiled, schemas)
    for c in clashes:
        await audit.write_anon(
            conn, trace=trace, via="worker", verb="federate.name_clash", target_kind="connection",
            target=name, outcome="error", detail=c,
        )
    if warnings:
        await audit.write_anon(
            conn, trace=trace, via="worker", verb="federate.guard_mismatch", target_kind="connection",
            target=name, outcome="error", detail={"warnings": warnings[:20]},
        )
    status = {
        "last_ok_at": _now_iso(), "last_attempt_at": _now_iso(), "last_error": None,
        "tool_count": len(kept), "resource_count": len(resources) + len(templates),
        "guarded": sum(1 for n in kept if any(g.matches(n) for g in compiled)),
        "clashes": clashes, "warnings": warnings,
    }
    await _write_status(conn, name, status)
    await audit.write_anon(
        conn, trace=trace, via="worker", verb="federate.refresh", target_kind="connection",
        target=name, outcome="ok", detail={"tools": len(kept), "clashes": len(clashes)},
    )
    return status


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


async def mcp_connections(conn: asyncpg.Connection) -> list[asyncpg.Record]:
    return await conn.fetch(
        f"SELECT {connections._COLUMNS} FROM connections WHERE type = 'mcp' ORDER BY name"
    )


async def refresh_due(conn: asyncpg.Connection) -> int:
    """The worker tick: refresh every mcp connection past its refresh_seconds."""
    n = 0
    for row in await mcp_connections(conn):
        cfg = json.loads(row["config"])
        every = int(cfg.get("refresh_seconds") or config.FEDERATION_REFRESH_SECONDS)
        status = _status(row)
        last = status.get("last_attempt_at")
        if last:
            from datetime import datetime

            age = time.time() - datetime.fromisoformat(last).timestamp()
            if age < every:
                continue
        await refresh(conn, row)
        n += 1
    return n


def health(row: asyncpg.Record) -> dict[str, Any]:
    """ok | stale | down for /health.federation and the screen."""
    cfg = json.loads(row["config"])
    status = _status(row)
    every = int(cfg.get("refresh_seconds") or config.FEDERATION_REFRESH_SECONDS)
    state = "down"
    if status.get("last_ok_at"):
        from datetime import datetime

        age = time.time() - datetime.fromisoformat(status["last_ok_at"]).timestamp()
        state = "ok" if age <= 3 * every and not status.get("last_error") else "stale"
    return {"name": row["name"], "status": state, "tool_count": status.get("tool_count", 0),
            "last_ok_at": status.get("last_ok_at"), "last_error": status.get("last_error")}


# -- the principal's view -----------------------------------------------------------


def block_for(principal: Principal, row: asyncpg.Record) -> tuple[str, dict[str, Any]] | None:
    """The federation block that governs this connection for this principal.

    None when the principal's grant names no such connection (FED-001) or
    its effective tier is under the connection's.
    """
    scope = principal.federation_scope or {}
    cfg = json.loads(row["config"])
    kind = cfg.get("resource_kind") or "other"
    if principal.effective_tier < row["tier"]:
        return None
    if kind == "other":
        spec = ((scope.get("mcp") or {}).get("connections") or {}).get(row["name"])
        if spec is None:
            return None
        return "mcp", dict(spec or {})
    block = scope.get(kind)
    if not block or row["name"] not in (block.get("connections") or []):
        return None
    return kind, block


async def visible_tools(
    conn: asyncpg.Connection, principal: Principal, exclude: set[str] | None = None
) -> list[dict[str, Any]]:
    """`tools/list`'s federated part. Never touches an upstream."""
    out: list[dict[str, Any]] = []
    exclude = exclude or set()
    for row in await mcp_connections(conn):
        found = block_for(principal, row)
        if found is None:
            continue
        _, block = found
        cfg = json.loads(row["config"])
        compiled = guards_mod.compile_guards(cfg.get("guards"))
        default_allow = cfg.get("default") == "allow"
        rows = await conn.fetch(
            "SELECT * FROM federated_tools WHERE connection = $1 ORDER BY upstream_name", row["name"]
        )
        for t in rows:
            if t["tool_name"] in exclude:
                continue
            if not guards_mod.tool_allowed(block, t["upstream_name"]):
                continue
            matching = [g for g in compiled if g.matches(t["upstream_name"])]
            if not matching and not default_allow:
                continue
            if not guards_mod.visible(matching, block, principal.effective_tier):
                continue
            schema = json.loads(t["input_schema"])
            tool = {
                "name": t["tool_name"],
                "description": f"{t['description'] or t['upstream_name']} [via {row['name']}]",
                "inputSchema": schema,
            }
            ann = json.loads(t["annotations"]) if t["annotations"] else {}
            ann["datumConnection"] = row["name"]
            tool["annotations"] = ann
            out.append(tool)
    return out


async def lookup(conn: asyncpg.Connection, name: str) -> tuple[asyncpg.Record, asyncpg.Record] | None:
    """(federated_tools row, connection row) for a gateway tool name."""
    t = await conn.fetchrow("SELECT * FROM federated_tools WHERE tool_name = $1", name)
    if t is None:
        return None
    c = await conn.fetchrow(
        f"SELECT {connections._COLUMNS} FROM connections WHERE name = $1", t["connection"]
    )
    if c is None:
        return None
    return t, c


async def visible_resources(conn: asyncpg.Connection, principal: Principal, templates: bool) -> list[dict[str, Any]]:
    out = []
    for row in await mcp_connections(conn):
        if block_for(principal, row) is None:
            continue
        rows = await conn.fetch(
            "SELECT * FROM federated_resources WHERE connection = $1 AND is_template = $2 ORDER BY upstream_uri",
            row["name"], templates,
        )
        for r in rows:
            item = {"name": r["name"] or r["upstream_uri"], "description": r["description"]}
            if r["mime_type"]:
                item["mimeType"] = r["mime_type"]
            item["uriTemplate" if templates else "uri"] = f"datum://{row['name']}/{r['upstream_uri']}"
            out.append(item)
    return out
