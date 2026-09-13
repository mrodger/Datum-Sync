"""Read routes behind the MCP Servers screen (spec/agent-auth-plane/08 §5).

Everything here is derived from `connections`, `federated_tools` and the
connection's `federation_status`; a refresh is the one write, and it is
the same refresh the worker tick runs.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Request

from datum_sync import audit, auth, db, principals
from datum_sync.auth import Principal
from datum_sync.errors import ApiError
from datum_sync.federation import catalogue, guards as guards_mod

router = APIRouter()
PROFILES_DIR = Path(__file__).parent / "profiles"


def _caller(request: Request) -> Principal:
    return request.state.principal


Caller = Depends(_caller)


def profiles() -> list[dict[str, Any]]:
    out = []
    for path in sorted(PROFILES_DIR.glob("*.json")):
        body = json.loads(path.read_text())
        out.append({"id": path.stem, "title": body.get("title", path.stem),
                    "description": body.get("description"), "config": body["config"]})
    return out


@router.get("/rest/v1/federation/profiles")
async def list_profiles(caller: Principal = Caller) -> dict[str, Any]:
    auth.require_admin(caller)
    return {"items": profiles()}


async def _as_principal(conn, caller: Principal, name: str | None) -> Principal | None:
    if not name:
        return None
    row = await principals._load(conn, name)
    if row is None:
        raise ApiError(404, "NOT_FOUND", f"no principal named {name!r}")
    return auth.principal_from_row(row, "token")


@router.get("/rest/v1/federation")
async def list_federation(request: Request, caller: Principal = Caller) -> dict[str, Any]:
    """Every mcp connection with its cached tools, guard coverage and, with
    `?as=<principal>`, whether that principal would see each tool and why not."""
    auth.require_admin(caller)
    async with db.pool().acquire() as conn:
        viewer = await _as_principal(conn, caller, request.query_params.get("as"))
        items = []
        for row in await catalogue.mcp_connections(conn):
            cfg = json.loads(row["config"])
            compiled = guards_mod.compile_guards(cfg.get("guards"))
            default_allow = cfg.get("default", "deny") == "allow"
            status = catalogue._status(row)
            found = catalogue.block_for(viewer, row) if viewer else None
            tools = []
            counts = {"guarded": 0, "unguarded": 0, "hidden": 0}
            for t in await conn.fetch(
                "SELECT * FROM federated_tools WHERE connection = $1 ORDER BY upstream_name", row["name"]
            ):
                matching = [g.label for g in compiled if g.matches(t["upstream_name"])]
                if matching:
                    counts["guarded"] += 1
                elif default_allow:
                    counts["unguarded"] += 1
                else:
                    counts["hidden"] += 1
                item: dict[str, Any] = {
                    "tool_name": t["tool_name"], "upstream_name": t["upstream_name"],
                    "description": t["description"], "guards": matching,
                    "listed": bool(matching) or default_allow,
                }
                if viewer:
                    item["visible"], item["why"] = _why(viewer, row, found, t, compiled, default_allow)
                tools.append(item)
            items.append({
                **catalogue.health(row),
                "tier": row["tier"], "resource_kind": cfg.get("resource_kind", "other"),
                "tool_prefix": cfg.get("tool_prefix") or row["name"], "default": cfg.get("default", "deny"),
                "refresh_seconds": cfg.get("refresh_seconds"),
                "coverage": counts, "clashes": status.get("clashes") or [],
                "warnings": status.get("warnings") or [], "last_attempt_at": status.get("last_attempt_at"),
                "tools": tools,
                "in_block": found is not None if viewer else None,
            })
    return {"items": items, "as": viewer.name if viewer else None}


def _why(viewer, row, found, t, compiled, default_allow) -> tuple[bool, str | None]:
    if found is None:
        if viewer.effective_tier < row["tier"]:
            return False, f"connection is tier {row['tier']}; effective tier {viewer.effective_tier}"
        return False, "connection is not in the principal's federation block"
    _, block = found
    if not guards_mod.tool_allowed(block, t["upstream_name"]):
        return False, "the block's tools allow/deny excludes it"
    matching = [g for g in compiled if g.matches(t["upstream_name"])]
    if not matching and not default_allow:
        return False, "unguarded, and the connection's default is deny"
    if not guards_mod.visible(matching, block, viewer.effective_tier):
        why = next(filter(None, (guards_mod.requires_met(g.requires, block, viewer.effective_tier)
                                 for g in matching)), "a guard's requirement is not met")
        return False, why
    return True, None


@router.post("/rest/v1/federation/{name}/refresh")
async def refresh_now(name: str, request: Request, caller: Principal = Caller) -> dict[str, Any]:
    auth.require_admin(caller)
    async with db.pool().acquire() as conn:
        row = await conn.fetchrow(
            f"SELECT {catalogue.connections._COLUMNS} FROM connections WHERE name = $1 AND type = 'mcp'", name
        )
        if row is None:
            raise ApiError(404, "NOT_FOUND", f"no mcp connection named {name!r}")
        trace = getattr(request.state, "trace", None) or audit.Trace.mint(None)
        status = await catalogue.refresh(conn, row, trace)
        row = await conn.fetchrow(
            f"SELECT {catalogue.connections._COLUMNS} FROM connections WHERE name = $1", name
        )
    return {**catalogue.health(row), "status_detail": status}
