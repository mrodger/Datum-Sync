"""Enrolment: a registration code in, a pending principal and a claim code out.

spec/agent-auth-plane/03 §4.1 and 07 §2. Two public endpoints, `POST /enrol`
and `POST /enrol/claim`, are the only unauthenticated writes in the system,
and the code is the credential: it is minted by a sponsor, stored as sha256,
counted per use, expiring, revocable, and rate-limited per code hash with the
same lockout the password form uses (auth._locked_for), keyed on the code
rather than the caller's address for the same reason the password lockout is
keyed on the name -- behind a tunnel every caller has one address.

What a code carries is a TEMPLATE: the widest authority a registrant may ask
for, bound to a parent. The registrant's `requested` tuple must narrow it
(grants.narrows). It may also send nothing and take the template whole.

Approval is a separate act (lifecycle.approve) by a person, unless the code
auto-approves -- which only tier 4 may mint. The token is never in the
approval response: the registrant exchanges the claim code for it, once,
so the credential reaches the process that will use it and nobody's screen.
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import asyncpg
from fastapi import APIRouter, Body, Depends, Request

from datum_sync import audit, auth, config, db, grants, principals, tokens
from datum_sync.auth import Principal
from datum_sync.errors import ApiError

router = APIRouter()


def _caller(request: Request) -> Principal:
    return request.state.principal


Caller = Depends(_caller)

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{1,63}$")
_RESERVED = re.compile(r"^(system|admin|superuser|_)")

_CODE_COLS = """
    c.id, c.label, c.auto_approve, c.max_uses, c.uses, c.expires_at, c.revoked_at,
    c.created_at, c.template,
    (SELECT name FROM service_accounts WHERE id = c.parent_id) AS parent,
    (SELECT name FROM service_accounts WHERE id = c.created_by) AS created_by
"""


def _public_code(row: asyncpg.Record) -> dict[str, Any]:
    return {
        "id": row["id"],
        "label": row["label"],
        "parent": row["parent"],
        "created_by": row["created_by"],
        "template": auth.json_of(row, "template"),
        "auto_approve": row["auto_approve"],
        "max_uses": row["max_uses"],
        "uses": row["uses"],
        "expires_at": row["expires_at"].isoformat(),
        "revoked_at": row["revoked_at"].isoformat() if row["revoked_at"] else None,
        "created_at": row["created_at"].isoformat(),
        "live": row["revoked_at"] is None
        and row["expires_at"] > datetime.now(timezone.utc)
        and row["uses"] < row["max_uses"],
    }


# -- codes --------------------------------------------------------------------------


async def mint_code(
    conn: asyncpg.Connection, caller: Principal, template: dict[str, Any], *,
    label: str | None, max_uses: int, expires_in_hours: int, auto_approve: bool,
    parent_name: str | None,
) -> tuple[asyncpg.Record, str]:
    """A code bound to `caller` (or a principal in its subtree) as the sponsor."""
    if auto_approve:
        auth.require_tier(caller, 4, "minting an auto-approving enrolment code")
    parent_row = await principals._load(conn, parent_name or caller.name)
    if parent_row["account_id"] != caller.account_id and caller.effective_tier < 4:
        raise ApiError(403, "FORBIDDEN", "a tier-3 sponsor may only mint codes for itself")
    # The template is a grant the sponsor is handing out: it must narrow the
    # sponsor's effective authority (and the parent's, when they differ).
    parent_effective = await principals.effective_authority(conn, parent_row)
    wider = grants.narrows(template, parent_effective)
    if wider:
        raise ApiError(
            400, "GRANT_NOT_NARROWER",
            f"template is wider than the sponsor in: {', '.join(wider)}", {"fields": wider},
        )
    # The same table that governs creating a child directly (03 §2): a
    # tier-3 sponsor hands out tier <= 2 within its own grant, a tier-4
    # administrator anything within theirs.
    principals._may_edit(caller, None, template, parent_row["account_id"])
    raw = auth.new_token()
    row = await conn.fetchrow(
        f"""
        WITH ins AS (
            INSERT INTO registration_codes
                (code_hash, created_by, parent_id, template, auto_approve, max_uses,
                 label, expires_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, now() + make_interval(hours => $8))
            RETURNING *
        )
        SELECT {_CODE_COLS} FROM ins c
        """,
        auth.hash_token(raw), caller.account_id, parent_row["account_id"],
        json.dumps(template), auto_approve, max_uses, label, expires_in_hours,
    )
    return row, raw


@router.post("/rest/v1/enrolment/codes", status_code=201)
async def create_code(
    request: Request, body: dict[str, Any] = Body(...), caller: Principal = Caller
) -> dict[str, Any]:
    auth.require_tier(caller, 3, "minting an enrolment code")
    template = principals.validate_authority(body.get("template") or {}, partial=False)
    if template["max_tier"] > 3:
        raise ApiError(400, "INVALID_PARAMETER", "an enrolled agent is at most tier 3",
                       {"field": "template.max_tier"})
    max_uses = body.get("max_uses", 1)
    hours = body.get("expires_in_hours", 72)
    for key, val in (("max_uses", max_uses), ("expires_in_hours", hours)):
        if not isinstance(val, int) or isinstance(val, bool) or val < 1:
            raise ApiError(400, "INVALID_PARAMETER", f"{key} must be a positive integer", {"field": key})
    async with db.pool().acquire() as conn:
        row, raw = await mint_code(
            conn, caller, template, label=body.get("label"), max_uses=max_uses,
            expires_in_hours=hours, auto_approve=bool(body.get("auto_approve", False)),
            parent_name=body.get("parent"),
        )
    await _audit(request, caller, "enrol.code.mint", row["parent"],
                 {"code_id": row["id"], "auto_approve": row["auto_approve"], "max_uses": max_uses})
    out = _public_code(row)
    out["code"] = raw
    return out


@router.get("/rest/v1/enrolment/codes")
async def list_codes(caller: Principal = Caller) -> dict[str, Any]:
    auth.require_tier(caller, 3, "listing enrolment codes")
    async with db.pool().acquire() as conn:
        if caller.effective_tier >= 4:
            rows = await conn.fetch(
                f"SELECT {_CODE_COLS} FROM registration_codes c ORDER BY c.created_at DESC"
            )
        else:
            rows = await conn.fetch(
                f"""SELECT {_CODE_COLS} FROM registration_codes c
                     WHERE c.parent_id = $1 ORDER BY c.created_at DESC""",
                caller.account_id,
            )
    return {"items": [_public_code(r) for r in rows]}


@router.delete("/rest/v1/enrolment/codes/{code_id}")
async def revoke_code(code_id: int, request: Request, caller: Principal = Caller) -> dict[str, Any]:
    auth.require_tier(caller, 3, "revoking an enrolment code")
    async with db.pool().acquire() as conn:
        row = await conn.fetchrow(
            f"SELECT {_CODE_COLS}, c.parent_id FROM registration_codes c WHERE c.id = $1", code_id
        )
        if row is None:
            raise ApiError(404, "NOT_FOUND", f"no enrolment code {code_id}")
        if caller.effective_tier < 4 and row["parent_id"] != caller.account_id:
            raise ApiError(403, "FORBIDDEN", "not your enrolment code")
        await conn.execute(
            "UPDATE registration_codes SET revoked_at = now() WHERE id = $1 AND revoked_at IS NULL",
            code_id,
        )
    await _audit(request, caller, "enrol.code.revoke", row["parent"], {"code_id": code_id})
    return {"id": code_id, "revoked": True}


@router.get("/rest/v1/enrolment/pending")
async def list_pending(caller: Principal = Caller) -> dict[str, Any]:
    auth.require_tier(caller, 3, "listing pending enrolments")
    async with db.pool().acquire() as conn:
        where = "sa.state = 'pending'"
        args: list[Any] = []
        if caller.effective_tier < 4:
            args.append(caller.account_id)
            where += " AND sa.parent_id = $1"
        rows = await conn.fetch(
            f"""
            SELECT {principals._COLS},
                   (SELECT cl.requested FROM registration_claims cl
                     WHERE cl.principal_id = sa.id ORDER BY cl.id DESC LIMIT 1) AS requested,
                   (SELECT rc.template FROM registration_claims cl
                      JOIN registration_codes rc ON rc.id = cl.code_id
                     WHERE cl.principal_id = sa.id ORDER BY cl.id DESC LIMIT 1) AS template
              FROM service_accounts sa
             WHERE {where}
             ORDER BY sa.created_at
            """,
            *args,
        )
    items = []
    for r in rows:
        item = principals.public(r)
        item["requested"] = auth.json_of(r, "requested")
        item["template"] = auth.json_of(r, "template")
        items.append(item)
    return {"items": items}


# -- the public half ----------------------------------------------------------------


def _lockout_key(code_hash: str) -> str:
    # Namespaced so an enrolment code and an account name can never share a
    # counter, and truncated because the key is only a bucket.
    return "enrol:" + code_hash[:16]


async def _audit_anon(request: Request, verb: str, name: str, outcome: str,
                      error_code: int | None, detail: dict | None) -> None:
    trace = getattr(request.state, "trace", None) or audit.Trace.mint(None)
    async with db.pool().acquire() as conn:
        await audit.write_anon(
            conn, trace=trace, via="rest", verb=verb, target_kind="principal",
            target=name, outcome=outcome, error_code=error_code, actor_name=name,
            detail=detail,
        )


async def _audit(request: Request, caller: Principal, verb: str, target: str,
                 detail: dict | None = None) -> None:
    trace = getattr(request.state, "trace", None) or audit.Trace.mint(None)
    async with db.pool().acquire() as conn:
        await audit.write(
            conn, trace=trace, principal=caller, via="rest", verb=verb,
            target_kind="principal", target=target, outcome="ok",
            governance=True, detail=detail,
        )


@router.post("/enrol", status_code=201)
async def enrol(request: Request, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """Public. A code, a name, optional metadata and a narrower request."""
    code = body.get("code")
    name = body.get("name")
    if not isinstance(code, str) or not code:
        raise ApiError(400, "INVALID_PARAMETER", "code is required", {"field": "code"})
    if not isinstance(name, str) or not _NAME_RE.match(name) or _RESERVED.match(name):
        raise ApiError(
            400, "INVALID_PARAMETER",
            "name must be 2-64 lowercase letters, digits, '_', '.' or '-', and not reserved",
            {"field": "name"},
        )
    metadata = body.get("metadata") or {}
    if not isinstance(metadata, dict):
        raise ApiError(400, "INVALID_PARAMETER", "metadata must be an object", {"field": "metadata"})
    requested_in = body.get("requested")
    if requested_in is not None and not isinstance(requested_in, dict):
        raise ApiError(400, "INVALID_PARAMETER", "requested must be an object", {"field": "requested"})

    code_hash = auth.hash_token(code)
    now = time.monotonic()
    retry_after = auth._locked_for(_lockout_key(code_hash), now)
    if retry_after:
        await _audit_anon(request, "enrol.request", name, "error", 429, {"reason": "too_many_attempts"})
        raise ApiError(
            429, "TOO_MANY_ATTEMPTS",
            f"too many failed attempts with this code; try again in {retry_after} seconds",
            headers={"Retry-After": str(retry_after)},
        )

    async with db.pool().acquire() as conn:
        async with conn.transaction():
            crow = await conn.fetchrow(
                """
                SELECT c.*, (SELECT name FROM service_accounts WHERE id = c.parent_id) AS parent
                  FROM registration_codes c WHERE c.code_hash = $1 FOR UPDATE
                """,
                code_hash,
            )
            live = (
                crow is not None and crow["revoked_at"] is None
                and crow["expires_at"] > datetime.now(timezone.utc)
                and crow["uses"] < crow["max_uses"]
            )
            if not live:
                # One answer for unknown, revoked, expired and used up: an
                # enrolment code is a credential and the difference is an
                # oracle. Counted against the lockout either way.
                auth._record_failure(_lockout_key(code_hash), now)
                await _audit_anon(request, "enrol.request", name, "error", 401,
                                  {"reason": "code_invalid"})
                raise ApiError(401, "ENROL_CODE_INVALID", "the enrolment code is not valid")

            template = auth.json_of(crow, "template")
            requested = principals.validate_authority(
                {**template, **(requested_in or {})}, partial=False
            )
            wider = grants.narrows(requested, template)
            if wider:
                raise ApiError(
                    400, "GRANT_NOT_NARROWER",
                    "requested authority is wider than the code allows in: " + ", ".join(wider),
                    {"fields": wider},
                )
            parent_row = await principals._load(conn, crow["parent"])
            await principals._check_parent(conn, parent_row, requested)

            state = "active" if crow["auto_approve"] else "pending"
            sets, args = principals._authority_sql(requested)
            try:
                principal_id = await conn.fetchval(
                    f"""
                    INSERT INTO service_accounts
                        (name, kind, parent_id, state, metadata, created_by,
                         review_due_at,
                         {', '.join(k for k in principals.AUTHORITY_FIELDS if k in requested)})
                    VALUES ($1, 'agent', $2, $3, $4, $5,
                            CASE WHEN $3 = 'active'
                                 THEN now() + make_interval(days => $6) END,
                            {', '.join(f'${i + 7}' for i in range(len(args)))})
                    RETURNING id
                    """,
                    name, crow["parent_id"], state, json.dumps(metadata),
                    f"enrol:{crow['parent']}", config.POLICY_REVIEW_INTERVAL_DAYS, *args,
                )
            except asyncpg.UniqueViolationError:
                raise ApiError(409, "ENROL_NAME_TAKEN", f"a principal named {name!r} exists") from None
            await conn.execute(
                "UPDATE registration_codes SET uses = uses + 1 WHERE id = $1", crow["id"]
            )
            claim_raw = auth.new_token()
            await conn.execute(
                """
                INSERT INTO registration_claims
                    (principal_id, code_id, claim_hash, requested, expires_at)
                VALUES ($1, $2, $3, $4, now() + make_interval(hours => $5))
                """,
                principal_id, crow["id"], auth.hash_token(claim_raw),
                json.dumps(requested), config.POLICY_CLAIM_TTL_HOURS,
            )
    auth._attempts.pop(_lockout_key(code_hash), None)
    await _audit_anon(request, "enrol.request", name, "ok", None,
                      {"parent": crow["parent"], "state": state, "code_id": crow["id"]})

    out: dict[str, Any] = {"name": name, "state": state, "parent": crow["parent"]}
    if state == "active":
        # Auto-approved: the claim is exchanged on the spot. One round trip
        # fewer for the registrant, and the same code path, so the token is
        # minted in exactly one place.
        claimed = await _claim(request, claim_raw)
        out.update(claimed)
    else:
        out["claim_code"] = claim_raw
        out["claim_expires_in_hours"] = config.POLICY_CLAIM_TTL_HOURS
        out["next"] = "wait for approval, then POST /enrol/claim with the claim_code"
    return out


@router.post("/enrol/claim")
async def claim(request: Request, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """Public. Exchange a claim code for the baseline token, once."""
    raw = body.get("claim_code")
    if not isinstance(raw, str) or not raw:
        raise ApiError(400, "INVALID_PARAMETER", "claim_code is required", {"field": "claim_code"})
    return await _claim(request, raw)


async def _claim(request: Request, raw: str) -> dict[str, Any]:
    claim_hash = auth.hash_token(raw)
    now = time.monotonic()
    retry_after = auth._locked_for(_lockout_key(claim_hash), now)
    if retry_after:
        raise ApiError(
            429, "TOO_MANY_ATTEMPTS",
            f"too many failed attempts; try again in {retry_after} seconds",
            headers={"Retry-After": str(retry_after)},
        )
    async with db.pool().acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                """
                SELECT cl.*, sa.name, sa.state, sa.max_tier
                  FROM registration_claims cl
                  JOIN service_accounts sa ON sa.id = cl.principal_id
                 WHERE cl.claim_hash = $1 FOR UPDATE OF cl
                """,
                claim_hash,
            )
            if row is None or row["claimed_at"] is not None or \
                    row["expires_at"] < datetime.now(timezone.utc):
                auth._record_failure(_lockout_key(claim_hash), now)
                await _audit_anon(request, "enrol.claim", row["name"] if row else "",
                                  "error", 401, {"reason": "claim_invalid"})
                raise ApiError(401, "CLAIM_INVALID", "the claim code is not valid")
            if row["state"] == "pending":
                raise ApiError(409, "PRINCIPAL_PENDING",
                               "not approved yet; try again once a sponsor has approved")
            if row["state"] != "active":
                raise ApiError(409, f"PRINCIPAL_{row['state'].upper()}",
                               f"the principal is {row['state']}")
            await conn.execute(
                "UPDATE registration_claims SET claimed_at = now() WHERE id = $1", row["id"]
            )
            cap = min(config.POLICY_BASELINE_TIER, row["max_tier"])
            _, token = await tokens.create(
                conn, row["principal_id"], "baseline", max_tier=cap
            )
            prow = await conn.fetchrow(
                f"SELECT {auth.PRINCIPAL_COLS} FROM service_accounts sa WHERE sa.id = $1",
                row["principal_id"],
            )
            principal = await auth.effective(
                conn, auth.principal_from_row(prow, "token", token_tier_cap=cap)
            )
    await _audit_anon(request, "enrol.claim", row["name"], "ok", None, {"token_tier_cap": cap})
    return {
        "name": row["name"],
        "state": "active",
        "token": token,
        "token_label": "baseline",
        "token_tier_cap": cap,
        "whoami": auth.principal_json(principal),
    }
