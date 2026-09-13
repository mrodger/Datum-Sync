"""Lifecycle: the states a principal moves through, and the verbs that move it.

spec/agent-auth-plane/03 §4. The state machine:

    pending --approve--> active --restrict--> restricted --restore--> active
    pending --reject---> rejected
    active  --disable--> disabled --enable--> active
    disabled/active --retire--> retired            (terminal; DELETE is tier 5)

Only `active` authenticates at full grant; `restricted` authenticates forced
to tier 1 (grants.restricted); the rest are refused by auth.effective with
the state as the code. Every transition here is a governance-flagged audit
row naming the actor, the target and the reason.

`restrict` stores the authority tuple in `restricted_from` and `restore`
puts back exactly that (PRIN-010) -- not the live row, which a later edit
could have moved, and not a recomputation, which would hide that it did.

`daily()` is the worker's housekeeping tick: it expires pending
registrations, restricts idle agents (never humans, spec 11 D-20) and is
the only writer that acts as `auth.SYSTEM_PRINCIPAL`.
"""
from __future__ import annotations

import json
from typing import Any

import asyncpg
from fastapi import APIRouter, Body, Depends, Request

from datum_sync import audit, auth, config, db, grants, principals, sessions, tokens
from datum_sync.auth import Principal
from datum_sync.errors import ApiError

router = APIRouter()


def _caller(request: Request) -> Principal:
    return request.state.principal


Caller = Depends(_caller)

# (from, verb) -> to. A verb not listed for a state is a 409, never a silent
# no-op: an operator who "restores" an active principal has misread the
# screen, and telling them so is cheaper than what they do next.
TRANSITIONS = {
    ("pending", "approve"): "active",
    ("pending", "reject"): "rejected",
    ("active", "restrict"): "restricted",
    ("restricted", "restore"): "active",
    ("active", "disable"): "disabled",
    ("restricted", "disable"): "disabled",
    ("disabled", "enable"): "active",
    ("active", "retire"): "retired",
    ("restricted", "retire"): "retired",
    ("disabled", "retire"): "retired",
    ("pending", "retire"): "retired",
    ("rejected", "retire"): "retired",
}


def _next_state(current: str, verb: str) -> str:
    try:
        return TRANSITIONS[(current, verb)]
    except KeyError:
        raise ApiError(
            409, "INVALID_TRANSITION",
            f"cannot {verb} a principal that is {current}",
            {"state": current, "verb": verb},
        ) from None


def _require_sponsor_or_admin(caller: Principal, row: asyncpg.Record, verb: str) -> None:
    """Tier 4, or the target's sponsor at tier >= 3."""
    if caller.effective_tier >= 4:
        return
    if caller.effective_tier >= 3 and row["parent_id"] == caller.account_id:
        return
    raise ApiError(403, "FORBIDDEN", f"{verb} requires tier 4 or the principal's sponsor")


async def _audit(request: Request, caller: Principal, verb: str, target: str,
                 detail: dict | None = None) -> None:
    trace = getattr(request.state, "trace", None) or audit.Trace.mint(None)
    async with db.pool().acquire() as conn:
        await audit.write(
            conn, trace=trace, principal=caller, via="rest", verb=verb,
            target_kind="principal", target=target, outcome="ok",
            governance=True, detail=detail,
        )


async def _revoke_everything(conn: asyncpg.Connection, account_id: int) -> dict[str, int]:
    """Every credential the principal holds: tokens, OAuth grants, sessions."""
    n_tokens = await tokens.revoke_all(conn, account_id)
    n_oauth = await conn.fetchval(
        """
        WITH hit AS (
            UPDATE oauth_tokens SET revoked_at = now()
             WHERE account_id = $1 AND revoked_at IS NULL
         RETURNING 1
        ) SELECT count(*) FROM hit
        """,
        account_id,
    )
    n_sessions = await sessions.end_for_principal(conn, account_id)
    return {"tokens": n_tokens, "oauth": n_oauth, "sessions": n_sessions}


async def _set_state(conn: asyncpg.Connection, account_id: int, state: str) -> None:
    await conn.execute(
        "UPDATE service_accounts SET state = $2 WHERE id = $1", account_id, state
    )


# -- transitions ---------------------------------------------------------------


async def approve(
    conn: asyncpg.Connection, caller: Principal, row: asyncpg.Record, edits: dict[str, Any]
) -> None:
    """pending -> active, with optional edits to the requested authority."""
    _next_state(row["state"], "approve")
    if edits:
        changes = principals.validate_authority(edits, partial=True)
        new = {**auth._row_authority(row), **changes}
        if row["kind"] == "agent" and new["max_tier"] > 3:
            raise ApiError(400, "INVALID_PARAMETER", "an agent is at most tier 3")
        if row["parent_id"] is not None:
            parent_row = await principals._load(conn, row["parent"])
            await principals._check_parent(conn, parent_row, new)
        principals._may_edit(caller, None, new, row["parent_id"])
        sets, args = principals._authority_sql(changes)
        if sets:
            await conn.execute(
                f"UPDATE service_accounts SET {', '.join(sets)} WHERE id = $1",
                row["account_id"], *args,
            )
    await conn.execute(
        """
        UPDATE service_accounts
           SET state = 'active',
               review_due_at = now() + make_interval(days => $2),
               last_reviewed_at = now(), last_reviewed_by = $3
         WHERE id = $1
        """,
        row["account_id"], config.POLICY_REVIEW_INTERVAL_DAYS, caller.name,
    )


async def reject(conn: asyncpg.Connection, row: asyncpg.Record, reason: str) -> None:
    _next_state(row["state"], "reject")
    await conn.execute(
        "UPDATE service_accounts SET state = 'rejected', restricted_reason = $2 WHERE id = $1",
        row["account_id"], reason,
    )
    await _revoke_everything(conn, row["account_id"])


async def restrict(conn: asyncpg.Connection, row: asyncpg.Record, reason: str) -> None:
    """active -> restricted. The authority tuple is snapshotted for restore."""
    _next_state(row["state"], "restrict")
    snapshot = auth._row_authority(row)
    await conn.execute(
        """
        UPDATE service_accounts
           SET state = 'restricted', restricted_from = $2, restricted_reason = $3
         WHERE id = $1
        """,
        row["account_id"], json.dumps(snapshot), reason,
    )
    # OAuth refresh grants are revoked (spec 03 §4): an elevated credential
    # must not outlive the restriction. Bearer tokens keep working, at tier 1,
    # so the agent can still ask why.
    await conn.execute(
        """
        UPDATE oauth_tokens SET revoked_at = now()
         WHERE account_id = $1 AND revoked_at IS NULL AND kind IN ('access', 'refresh')
        """,
        row["account_id"],
    )
    # Live sessions end too: the next initialize is admitted at the
    # restricted limit and sees the restricted tools (SESS-003).
    await sessions.end_for_principal(conn, row["account_id"], reason="revoked")


async def restore(conn: asyncpg.Connection, row: asyncpg.Record) -> None:
    """restricted -> active, putting back exactly the stored tuple (PRIN-010)."""
    _next_state(row["state"], "restore")
    stored = auth.json_of(row, "restricted_from")
    if stored is None:
        raise ApiError(409, "NO_SNAPSHOT", "nothing stored to restore from")
    sets, args = principals._authority_sql(stored)
    await conn.execute(
        f"""
        UPDATE service_accounts
           SET state = 'active', restricted_from = NULL, restricted_reason = NULL,
               {', '.join(sets)}
         WHERE id = $1
        """,
        row["account_id"], *args,
    )


async def retire(
    conn: asyncpg.Connection, row: asyncpg.Record, cascade: bool
) -> list[str]:
    """-> retired, credentials revoked, rows kept (PRIN-011). Returns names retired."""
    _next_state(row["state"], "retire")
    children = await conn.fetch(
        "SELECT id, name, state FROM service_accounts WHERE parent_id = $1", row["account_id"]
    )
    live = [c["name"] for c in children if c["state"] != "retired"]
    if live and not cascade:
        raise ApiError(
            409, "CHILDREN_ACTIVE",
            "retire the children first, or pass cascade=true: " + ", ".join(live),
            {"names": live},
        )
    retired: list[str] = []
    for c in children:
        if c["state"] != "retired":
            crow = await principals._load(conn, c["name"])
            retired += await retire(conn, crow, cascade=True)
    await _revoke_everything(conn, row["account_id"])
    await _set_state(conn, row["account_id"], "retired")
    retired.append(row["name"])
    return retired


async def review(
    conn: asyncpg.Connection, caller: Principal, row: asyncpg.Record,
    edits: dict[str, Any], note: str | None,
) -> None:
    """Record a review; optionally narrow; reset the due date."""
    if row["state"] not in ("active", "restricted"):
        raise ApiError(409, "INVALID_TRANSITION", f"cannot review a principal that is {row['state']}")
    if edits:
        changes = principals.validate_authority(edits, partial=True)
        current = auth._row_authority(row)
        new = {**current, **changes}
        wider = grants.narrows(new, current)
        if wider:
            raise ApiError(
                400, "REVIEW_MUST_NARROW",
                "a review may narrow a grant, never widen it: " + ", ".join(wider),
                {"fields": wider},
            )
        principals._may_edit(caller, await principals.effective_authority(conn, row), new, row["parent_id"])
        sets, args = principals._authority_sql(changes)
        await conn.execute(
            f"UPDATE service_accounts SET {', '.join(sets)} WHERE id = $1",
            row["account_id"], *args,
        )
    await conn.execute(
        """
        UPDATE service_accounts
           SET review_due_at = now() + make_interval(days => $2),
               last_reviewed_at = now(), last_reviewed_by = $3,
               metadata = metadata || jsonb_build_object('last_review_note', $4::text)
         WHERE id = $1
        """,
        row["account_id"], config.POLICY_REVIEW_INTERVAL_DAYS, caller.name, note or "",
    )


# -- routes ------------------------------------------------------------------------


def _reason(body: dict[str, Any]) -> str:
    reason = body.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        raise ApiError(400, "INVALID_PARAMETER", "reason is required", {"field": "reason"})
    return reason.strip()


@router.post("/rest/v1/principals/{name}/approve")
async def approve_route(
    name: str, request: Request, body: dict[str, Any] = Body(default_factory=dict),
    caller: Principal = Caller,
) -> dict[str, Any]:
    auth.require_tier(caller, 3, "approving a principal")
    async with db.pool().acquire() as conn:
        row = await principals._load(conn, name)
        _require_sponsor_or_admin(caller, row, "approve")
        async with conn.transaction():
            await approve(conn, caller, row, body.get("edits") or {})
        row = await principals._load(conn, name)
        eff = await principals.effective_authority(conn, row)
    await _audit(request, caller, "principal.approve", name,
                 {"edited": sorted(body.get("edits") or {})})
    return principals.public(row, eff)


@router.post("/rest/v1/principals/{name}/reject")
async def reject_route(
    name: str, request: Request, body: dict[str, Any] = Body(...), caller: Principal = Caller
) -> dict[str, Any]:
    auth.require_tier(caller, 3, "rejecting a principal")
    reason = _reason(body)
    async with db.pool().acquire() as conn:
        row = await principals._load(conn, name)
        _require_sponsor_or_admin(caller, row, "reject")
        async with conn.transaction():
            await reject(conn, row, reason)
        row = await principals._load(conn, name)
    await _audit(request, caller, "principal.reject", name, {"reason": reason})
    return principals.public(row)


@router.post("/rest/v1/principals/{name}/restrict")
async def restrict_route(
    name: str, request: Request, body: dict[str, Any] = Body(...), caller: Principal = Caller
) -> dict[str, Any]:
    auth.require_tier(caller, 3, "restricting a principal")
    reason = _reason(body)
    async with db.pool().acquire() as conn:
        row = await principals._load(conn, name)
        _require_sponsor_or_admin(caller, row, "restrict")
        if row["account_id"] == caller.account_id:
            raise ApiError(400, "INVALID_PARAMETER", "cannot restrict your own principal")
        async with conn.transaction():
            await restrict(conn, row, reason)
        row = await principals._load(conn, name)
        eff = await principals.effective_authority(conn, row)
    await _audit(request, caller, "principal.restrict", name, {"reason": reason})
    return principals.public(row, eff)


@router.post("/rest/v1/principals/{name}/restore")
async def restore_route(name: str, request: Request, caller: Principal = Caller) -> dict[str, Any]:
    auth.require_tier(caller, 3, "restoring a principal")
    async with db.pool().acquire() as conn:
        row = await principals._load(conn, name)
        _require_sponsor_or_admin(caller, row, "restore")
        async with conn.transaction():
            await restore(conn, row)
        row = await principals._load(conn, name)
        eff = await principals.effective_authority(conn, row)
    await _audit(request, caller, "principal.restore", name)
    return principals.public(row, eff)


@router.post("/rest/v1/principals/{name}/retire")
async def retire_route(name: str, request: Request, caller: Principal = Caller) -> dict[str, Any]:
    auth.require_tier(caller, 4, "retiring a principal")
    cascade = request.query_params.get("cascade", "").lower() in ("1", "true", "yes")
    async with db.pool().acquire() as conn:
        row = await principals._load(conn, name)
        if row["account_id"] == caller.account_id:
            raise ApiError(400, "INVALID_PARAMETER", "cannot retire your own principal")
        async with conn.transaction():
            retired = await retire(conn, row, cascade)
        row = await principals._load(conn, name)
    await _audit(request, caller, "principal.retire", name, {"retired": retired})
    out = principals.public(row)
    out["retired"] = retired
    return out


@router.post("/rest/v1/principals/{name}/review")
async def review_route(
    name: str, request: Request, body: dict[str, Any] = Body(default_factory=dict),
    caller: Principal = Caller,
) -> dict[str, Any]:
    auth.require_tier(caller, 3, "reviewing a principal")
    async with db.pool().acquire() as conn:
        row = await principals._load(conn, name)
        _require_sponsor_or_admin(caller, row, "review")
        async with conn.transaction():
            await review(conn, caller, row, body.get("narrow") or {}, body.get("note"))
        row = await principals._load(conn, name)
        eff = await principals.effective_authority(conn, row)
    await _audit(request, caller, "principal.review", name,
                 {"narrowed": sorted(body.get("narrow") or {})})
    return principals.public(row, eff)


# -- the daily tick ------------------------------------------------------------


async def daily(conn: asyncpg.Connection) -> dict[str, Any]:
    """Housekeeping. Idempotent; safe to run more often than daily.

    Returns what it did, for the worker's log line. Each action is one
    governance audit row under a trace of its own, as SYSTEM_PRINCIPAL.
    """
    trace = audit.Trace.mint(None)
    out: dict[str, Any] = {"expired": [], "restricted": [], "reviews_overdue": 0}

    expired = await conn.fetch(
        """
        UPDATE service_accounts
           SET state = 'rejected', restricted_reason = 'expired: not approved in time'
         WHERE state = 'pending'
           AND created_at < now() - make_interval(days => $1)
        RETURNING id, name
        """,
        config.POLICY_PENDING_TTL_DAYS,
    )
    for r in expired:
        await _revoke_everything(conn, r["id"])
        out["expired"].append(r["name"])
        await audit.write(
            conn, trace=trace, principal=auth.SYSTEM_PRINCIPAL, via="worker",
            verb="principal.auto_reject", target_kind="principal", target=r["name"],
            outcome="ok", governance=True, detail={"reason": "pending_ttl"},
        )

    # Idle agents, never humans (D-20). `last_used_at` is bumped by every
    # token resolution; an agent that has never authenticated is judged from
    # its creation date.
    idle = await conn.fetch(
        f"""
        SELECT {auth.PRINCIPAL_COLS}
          FROM service_accounts sa
         WHERE sa.kind = 'agent' AND sa.state = 'active'
           AND coalesce(sa.last_used_at, sa.created_at) < now() - make_interval(days => $1)
        """,
        config.POLICY_INACTIVITY_RESTRICT_DAYS,
    )
    for r in idle:
        snapshot = auth._row_authority(r)
        await conn.execute(
            """
            UPDATE service_accounts
               SET state = 'restricted', restricted_from = $2,
                   restricted_reason = $3
             WHERE id = $1 AND state = 'active'
            """,
            r["account_id"], json.dumps(snapshot),
            f"inactive for {config.POLICY_INACTIVITY_RESTRICT_DAYS} days",
        )
        out["restricted"].append(r["name"])
        await audit.write(
            conn, trace=trace, principal=auth.SYSTEM_PRINCIPAL, via="worker",
            verb="principal.auto_restrict", target_kind="principal", target=r["name"],
            outcome="ok", governance=True,
            detail={"reason": "inactivity", "days": config.POLICY_INACTIVITY_RESTRICT_DAYS},
        )

    out["reviews_overdue"] = await reviews_overdue(conn)
    return out


async def reviews_overdue(conn: asyncpg.Connection) -> int:
    return await conn.fetchval(
        """
        SELECT count(*) FROM service_accounts
         WHERE state IN ('active', 'restricted') AND review_due_at < now()
        """
    )


async def pending_count(conn: asyncpg.Connection) -> int:
    return await conn.fetchval("SELECT count(*) FROM service_accounts WHERE state = 'pending'")
