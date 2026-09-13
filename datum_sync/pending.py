"""Approval-gated federated calls (spec/agent-auth-plane/05 §5).

A guard with `requires.approval` whose label the principal's block lists in
`approval_required` does not forward; it parks the call here with the
requester's grant snapshot. A person approves or rejects on the Approvals
screen; approval forwards the call **under the snapshot** (FED-010) and
stores the result for the requester alone to fetch.

Who may decide: tier 4, or the requester's sponsor when the sponsor holds
the same connection in a block that covers the requester's (REV-002). An
expired request cannot be approved (REV-003): the tick marks it, and the
agent asks again.
"""
from __future__ import annotations

import json
from typing import Any

import asyncpg
from fastapi import APIRouter, Body, Depends, Request

from datum_sync import audit, auth, config, db, grants
from datum_sync.auth import Principal
from datum_sync.errors import ApiError

router = APIRouter()


def _caller(request: Request) -> Principal:
    return request.state.principal


Caller = Depends(_caller)


def snapshot(principal: Principal) -> dict[str, Any]:
    """Same shape as jobs.grant_snapshot: the authority a call runs under."""
    return {
        **principal.authority(),
        "principal_name": principal.name,
        "principal_id": principal.account_id,
        "kind": principal.kind,
        "parent_id": principal.parent_id,
        "effective_tier": principal.effective_tier,
    }


def snapshot_principal(snap: dict[str, Any]) -> Principal:
    """The requester as it was at request time, for the guard walk."""
    tier = int(snap.get("effective_tier") or 1)
    return Principal(
        account_id=int(snap["principal_id"]), name=str(snap["principal_name"]),
        max_tier=tier, repo_scope=list(snap.get("repo_scope") or []), is_admin=tier >= 4,
        source="snapshot", vault_scope=snap.get("vault_scope"), rate_limit_per_min=None,
        proxy_grants=list(snap.get("proxy_grants") or []), kind=str(snap.get("kind") or "agent"),
        parent_id=snap.get("parent_id"), limits=dict(snap.get("limits") or {}),
        federation_scope=snap.get("federation_scope"),
    )


async def _live(conn: asyncpg.Connection, account_id: int) -> Principal:
    """The requester as it is *now*. Exists so the FED-010 break case can
    name the wrong thing to do; `approve` never calls it."""
    from datum_sync import tokens

    row = await conn.fetchrow(
        f"SELECT {tokens._ACCOUNT_COLS} FROM service_accounts sa WHERE id = $1", account_id
    )
    return auth.principal_from_row(row, "snapshot")


def public(row: asyncpg.Record, with_result: bool = False) -> dict[str, Any]:
    def when(v):
        return v.isoformat() if v else None

    snap = auth.json_of(row, "grant_snapshot") or {}
    out = {
        "id": row["id"], "connection": row["connection"], "tool": row["tool"],
        "args": auth.json_of(row, "args"), "label": row["label"],
        "requested_by": row["requested_name"], "requested_at": when(row["requested_at"]),
        "expires_at": when(row["expires_at"]), "status": row["status"],
        "decided_by": row["decided_name"], "decided_at": when(row["decided_at"]),
        "reason": row["reason"], "sponsor_id": snap.get("parent_id"),
        "requester_tier": snap.get("effective_tier"),
    }
    if with_result:
        out["result"] = auth.json_of(row, "result")
    return out


# -- the request ------------------------------------------------------------------


async def request(
    conn: asyncpg.Connection, principal: Principal, connection: str, tool: str,
    args: dict[str, Any], label: str, trace: audit.Trace,
) -> dict[str, Any]:
    row = await conn.fetchrow(
        """
        INSERT INTO pending_calls
            (connection, tool, args, label, requested_by, requested_name, expires_at,
             session_id, trace_id, grant_snapshot)
        VALUES ($1, $2, $3, $4, $5, $6, now() + make_interval(hours => $7), $8, $9, $10)
        RETURNING *
        """,
        connection, tool, json.dumps(args), label, principal.account_id, principal.name,
        config.POLICY_PENDING_CALL_TTL_HOURS,
        trace.session_id, trace.id, json.dumps(snapshot(principal)),
    )
    await audit.write(
        conn, trace=trace, principal=principal, via="mcp", verb="federate.pending",
        target_kind="tool", target=f"{connection}:{tool}", outcome="ok", governance=True,
        session_id=trace.session_id, detail={"pending_id": row["id"], "label": label},
    )
    return public(row)


def handle(row_public: dict[str, Any]) -> dict[str, Any]:
    """What the agent gets back from tools/call: not an error, a wait."""
    body = {"status": "pending_approval", "pending_id": row_public["id"],
            "label": row_public["label"], "expires_at": row_public["expires_at"]}
    text = (f"Held for approval ({row_public['label']}): pending call {row_public['id']}. "
            "A person decides on the Approvals screen; call pending_status with this id, "
            "then pending_result for the outcome.")
    return {"content": [{"type": "text", "text": text}], "structuredContent": body, "isError": False}


# -- decisions ---------------------------------------------------------------------


async def _load(conn: asyncpg.Connection, pending_id: int) -> asyncpg.Record:
    row = await conn.fetchrow("SELECT * FROM pending_calls WHERE id = $1", pending_id)
    if row is None:
        raise ApiError(404, "NOT_FOUND", f"no pending call {pending_id}")
    return row


async def _may_decide(conn: asyncpg.Connection, caller: Principal, row: asyncpg.Record) -> None:
    """Tier 4, or the requester's sponsor holding the connection in a block
    that covers the requester's (REV-002)."""
    if caller.effective_tier >= 4:
        return
    snap = auth.json_of(row, "grant_snapshot") or {}
    if caller.effective_tier >= 3 and snap.get("parent_id") == caller.account_id:
        from datum_sync.federation import catalogue

        connection = await conn.fetchrow(
            f"SELECT {catalogue.connections._COLUMNS} FROM connections WHERE name = $1", row["connection"]
        )
        mine = catalogue.block_for(caller, connection) if connection else None
        theirs = catalogue.block_for(snapshot_principal(snap), connection) if connection else None
        if mine and theirs and not grants._fed_narrows({theirs[0]: theirs[1]}, {mine[0]: mine[1]}):
            return
    raise ApiError(
        403, "FORBIDDEN",
        "deciding a pending call needs tier 4, or the requester's sponsor holding the same connection",
    )


async def approve(
    conn: asyncpg.Connection, caller: Principal, pending_id: int, trace: audit.Trace
) -> dict[str, Any]:
    """Forward the call once, under the requester's snapshot; store the result."""
    from datum_sync import mcp

    row = await _load(conn, pending_id)
    await _may_decide(conn, caller, row)
    if row["status"] != "pending":
        # Decided, executed or expired: never forwarded twice (FED-009).
        raise ApiError(409, "ALREADY_DECIDED", f"pending call {pending_id} is {row['status']}")
    if row["expires_at"] < await conn.fetchval("SELECT now()"):
        raise ApiError(409, "EXPIRED", "the request has expired; the agent must ask again")
    await conn.execute(
        """
        UPDATE pending_calls SET status = 'approved', decided_by = $2, decided_name = $3, decided_at = now()
         WHERE id = $1
        """,
        pending_id, caller.account_id, caller.name,
    )
    await audit.write(
        conn, trace=trace, principal=caller, via="rest", verb="federate.approve", target_kind="tool",
        target=f"{row['connection']}:{row['tool']}", outcome="ok", governance=True,
        detail={"pending_id": pending_id, "requested_by": row["requested_name"]},
    )
    # The requester as it was, not as it is (FED-010): the approver saw and
    # approved this call under that grant.
    requester = snapshot_principal(auth.json_of(row, "grant_snapshot") or {})
    result = await mcp.execute_approved(
        conn, requester, row["connection"], row["tool"], auth.json_of(row, "args") or {}, trace,
    )
    status = "failed" if result.get("isError") else "executed"
    await conn.execute(
        "UPDATE pending_calls SET status = $2, result = $3 WHERE id = $1",
        pending_id, status, json.dumps(result),
    )
    await audit.write(
        conn, trace=trace, principal=caller, via="rest", verb="federate.execute", target_kind="tool",
        target=f"{row['connection']}:{row['tool']}", outcome="error" if status == "failed" else "ok",
        detail={"pending_id": pending_id, "requested_by": row["requested_name"]},
    )
    return public(await _load(conn, pending_id))


async def reject(
    conn: asyncpg.Connection, caller: Principal, pending_id: int, reason: str, trace: audit.Trace
) -> dict[str, Any]:
    row = await _load(conn, pending_id)
    await _may_decide(conn, caller, row)
    if row["status"] != "pending":
        raise ApiError(409, "ALREADY_DECIDED", f"pending call {pending_id} is {row['status']}")
    await conn.execute(
        """
        UPDATE pending_calls SET status = 'rejected', decided_by = $2, decided_name = $3, decided_at = now(),
               reason = $4
         WHERE id = $1
        """,
        pending_id, caller.account_id, caller.name, reason,
    )
    await audit.write(
        conn, trace=trace, principal=caller, via="rest", verb="federate.reject", target_kind="tool",
        target=f"{row['connection']}:{row['tool']}", outcome="denied", governance=True,
        detail={"pending_id": pending_id, "requested_by": row["requested_name"], "reason": reason},
    )
    return public(await _load(conn, pending_id))


async def expire(conn: asyncpg.Connection) -> int:
    """The tick: pending past expires_at -> expired."""
    rows = await conn.fetch(
        "UPDATE pending_calls SET status = 'expired' WHERE status = 'pending' AND expires_at < now() RETURNING id"
    )
    return len(rows)


# -- the requester's view (MCP built-ins) --------------------------------------------


async def for_requester(conn: asyncpg.Connection, principal: Principal, pending_id: Any) -> asyncpg.Record:
    """The row, for its requester only. Anyone else gets not-found."""
    try:
        pid = int(pending_id)
    except (TypeError, ValueError):
        raise ApiError(400, "INVALID_PARAMETER", "pending_id is an integer")
    row = await conn.fetchrow(
        "SELECT * FROM pending_calls WHERE id = $1 AND requested_by = $2", pid, principal.account_id
    )
    if row is None:
        raise ApiError(404, "NOT_FOUND", f"no pending call {pid}")
    return row


async def pending_count(conn: asyncpg.Connection) -> int:
    return await conn.fetchval(
        "SELECT count(*) FROM pending_calls WHERE status = 'pending' AND expires_at > now()"
    )


# -- REST --------------------------------------------------------------------------


@router.get("/rest/v1/pending-calls")
async def list_pending(request: Request, caller: Principal = Caller) -> dict[str, Any]:
    auth.require_tier(caller, 3, "listing pending calls")
    status = request.query_params.get("status", "pending")
    where, args = ["true"], []
    if status == "pending":
        where.append("status = 'pending' AND expires_at > now()")
    elif status == "decided":
        where.append("status <> 'pending'")
    if caller.effective_tier < 4:
        args.append(caller.account_id)
        where.append(f"(requested_by = ${len(args)} OR (grant_snapshot->>'parent_id')::int = ${len(args)})")
    async with db.pool().acquire() as conn:
        rows = await conn.fetch(
            f"SELECT * FROM pending_calls WHERE {' AND '.join(where)} ORDER BY requested_at DESC LIMIT 200",
            *args,
        )
    return {"items": [public(r) for r in rows]}


@router.post("/rest/v1/pending-calls/{pending_id}/approve")
async def approve_route(pending_id: int, request: Request, caller: Principal = Caller) -> dict[str, Any]:
    auth.require_tier(caller, 3, "approving a pending call")
    trace = getattr(request.state, "trace", None) or audit.Trace.mint(None)
    async with db.pool().acquire() as conn:
        return await approve(conn, caller, pending_id, trace)


@router.post("/rest/v1/pending-calls/{pending_id}/reject")
async def reject_route(
    pending_id: int, request: Request, body: dict[str, Any] = Body(default_factory=dict),
    caller: Principal = Caller,
) -> dict[str, Any]:
    auth.require_tier(caller, 3, "rejecting a pending call")
    reason = body.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        raise ApiError(400, "INVALID_PARAMETER", "reason is required", {"field": "reason"})
    trace = getattr(request.state, "trace", None) or audit.Trace.mint(None)
    async with db.pool().acquire() as conn:
        return await reject(conn, caller, pending_id, reason.strip(), trace)
