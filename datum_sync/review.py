"""The review queue and the activity rollup (spec/agent-auth-plane/07 §5, 08 §4, §7).

One list of everything waiting for a person, oldest first, each item with
the link that resolves it. Computed live: every source is a small indexed
query, and a queue that lags its sources is worse than no queue.

The activity rollup is counts from `audit_log`. Visible to the principal
itself, its sponsor, and tier 4 (REV-001); targets only at tier 4, because
a target is a repository name or a path and a sponsor sees counts.
"""
from __future__ import annotations

import json
from typing import Any

import asyncpg
from fastapi import APIRouter, Depends, Request

from datum_sync import auth, db, principals
from datum_sync.auth import Principal
from datum_sync.errors import ApiError

router = APIRouter()


def _caller(request: Request) -> Principal:
    return request.state.principal


Caller = Depends(_caller)


def _age(conn_now, then) -> int:
    return int((conn_now - then).total_seconds()) if then else 0


async def queue(conn: asyncpg.Connection, caller: Principal) -> list[dict[str, Any]]:
    from datum_sync.federation import catalogue

    now = await conn.fetchval("SELECT now()")
    mine = caller.effective_tier < 4
    subtree = "true"
    args: list[Any] = []
    if mine:
        args.append(caller.account_id)
        subtree = "(sa.parent_id = $1 OR sa.id = $1)"
    items: list[dict[str, Any]] = []

    for r in await conn.fetch(
        f"SELECT sa.name, sa.created_at FROM service_accounts sa WHERE sa.state = 'pending' AND {subtree}", *args
    ):
        items.append({"kind": "enrolment", "age": _age(now, r["created_at"]), "principal": r["name"],
                      "label": f"{r['name']} is waiting for approval", "link": "#/enrolment?tab=pending",
                      "at": r["created_at"].isoformat()})
    for r in await conn.fetch(
        f"""
        SELECT d.user_code, d.scope, d.requested_at, sa.name FROM oauth_device_codes d
          JOIN service_accounts sa ON sa.id = d.principal_id
         WHERE d.decision IS NULL AND d.expires_at > now() AND {subtree}
        """, *args
    ):
        items.append({"kind": "elevation", "age": _age(now, r["requested_at"]), "principal": r["name"],
                      "label": f"{r['name']} asks for {r['scope']} ({r['user_code']})",
                      "link": f"#/approvals?code={r['user_code']}", "at": r["requested_at"].isoformat()})
    for r in await conn.fetch(
        f"""
        SELECT p.id, p.tool, p.label, p.requested_at, sa.name FROM pending_calls p
          JOIN service_accounts sa ON sa.id = p.requested_by
         WHERE p.status = 'pending' AND p.expires_at > now() AND {subtree}
        """, *args
    ):
        items.append({"kind": "call", "age": _age(now, r["requested_at"]), "principal": r["name"],
                      "label": f"{r['name']} wants {r['tool']} ({r['label']})",
                      "link": "#/approvals?tab=calls", "at": r["requested_at"].isoformat()})
    for r in await conn.fetch(
        f"""
        SELECT sa.name, sa.review_due_at FROM service_accounts sa
         WHERE sa.state IN ('active', 'restricted') AND sa.review_due_at < now() AND {subtree}
        """, *args
    ):
        items.append({"kind": "review", "age": _age(now, r["review_due_at"]), "principal": r["name"],
                      "label": f"{r['name']} is overdue for review", "link": f"#/admin/{r['name']}",
                      "at": r["review_due_at"].isoformat()})
    for r in await conn.fetch(
        f"""
        SELECT sa.name, coalesce(sa.last_used_at, sa.created_at) AS idle_since, sa.restricted_reason
          FROM service_accounts sa
         WHERE sa.state = 'restricted' AND sa.restricted_reason LIKE 'inactive for%' AND {subtree}
        """, *args
    ):
        items.append({"kind": "inactive", "age": _age(now, r["idle_since"]), "principal": r["name"],
                      "label": f"{r['name']} was restricted for inactivity", "link": f"#/admin/{r['name']}",
                      "at": r["idle_since"].isoformat()})
    if not mine:
        for row in await catalogue.mcp_connections(conn):
            h = catalogue.health(row)
            status = catalogue._status(row)
            if h["status"] != "ok":
                items.append({"kind": "upstream", "age": 0, "principal": None,
                              "label": f"{row['name']} is {h['status']}" + (f": {h['last_error']}" if h["last_error"] else ""),
                              "link": "#/mcp", "at": h["last_ok_at"]})
            for c in status.get("clashes") or []:
                items.append({"kind": "clash", "age": 0, "principal": None,
                              "label": f"{row['name']}: {c['upstream']} clashes with {c['tool_name']}",
                              "link": "#/mcp", "at": status.get("last_ok_at")})
    items.sort(key=lambda x: -x["age"])
    return items


@router.get("/rest/v1/review")
async def review_queue(caller: Principal = Caller) -> dict[str, Any]:
    auth.require_tier(caller, 3, "reading the review queue")
    async with db.pool().acquire() as conn:
        items = await queue(conn, caller)
    counts: dict[str, int] = {}
    for it in items:
        counts[it["kind"]] = counts.get(it["kind"], 0) + 1
    return {"items": items, "counts": counts, "total": len(items)}


# -- activity ----------------------------------------------------------------------

WINDOWS = {"7d": 7, "30d": 30}


async def activity(conn: asyncpg.Connection, row: asyncpg.Record, days: int, with_targets: bool) -> dict[str, Any]:
    name = row["name"]
    by_via = await conn.fetch(
        """
        SELECT via, count(*) AS n, count(*) FILTER (WHERE outcome = 'denied') AS denied,
               count(*) FILTER (WHERE outcome = 'error') AS errors
          FROM audit_log WHERE actor_name = $1 AND created_at > now() - make_interval(days => $2)
         GROUP BY via ORDER BY n DESC
        """, name, days)
    by_kind = await conn.fetch(
        """
        SELECT coalesce(target_kind, '(none)') AS kind, count(*) AS n,
               count(*) FILTER (WHERE outcome = 'denied') AS denied
          FROM audit_log WHERE actor_name = $1 AND created_at > now() - make_interval(days => $2)
         GROUP BY kind ORDER BY n DESC
        """, name, days)
    totals = await conn.fetchrow(
        """
        SELECT count(*) AS n, count(*) FILTER (WHERE outcome = 'denied') AS denied,
               count(*) FILTER (WHERE governance) AS governance, max(created_at) AS last_seen,
               count(DISTINCT session_id) FILTER (WHERE session_id IS NOT NULL) AS sessions
          FROM audit_log WHERE actor_name = $1 AND created_at > now() - make_interval(days => $2)
        """, name, days)
    out = {
        "principal": name, "window_days": days,
        "total": totals["n"], "denied": totals["denied"], "governance": totals["governance"],
        "sessions": totals["sessions"], "last_seen": totals["last_seen"].isoformat() if totals["last_seen"] else None,
        "by_surface": [{"via": r["via"], "count": r["n"], "denied": r["denied"], "errors": r["errors"]} for r in by_via],
        "by_kind": [{"kind": r["kind"], "count": r["n"], "denied": r["denied"]} for r in by_kind],
    }
    if with_targets:
        top = await conn.fetch(
            """
            SELECT target_kind, target, count(*) AS n
              FROM audit_log WHERE actor_name = $1 AND target IS NOT NULL
               AND created_at > now() - make_interval(days => $2)
             GROUP BY target_kind, target ORDER BY n DESC LIMIT 10
            """, name, days)
        out["top_targets"] = [{"kind": r["target_kind"], "target": r["target"], "count": r["n"]} for r in top]
    return out


@router.get("/rest/v1/principals/{name}/activity")
async def activity_route(name: str, request: Request, caller: Principal = Caller) -> dict[str, Any]:
    window = request.query_params.get("window", "7d")
    if window not in WINDOWS:
        raise ApiError(400, "INVALID_PARAMETER", "window is 7d or 30d", {"field": "window"})
    async with db.pool().acquire() as conn:
        row = await principals._load(conn, name)
        # Self, sponsor, or tier 4 (REV-001). Anyone else is told nothing,
        # not even that the principal exists.
        if not (caller.account_id == row["account_id"] or row["parent_id"] == caller.account_id
                or caller.effective_tier >= 4):
            raise ApiError(404, "NOT_FOUND", f"no principal named {name!r}")
        return await activity(conn, row, WINDOWS[window], with_targets=caller.effective_tier >= 4)
