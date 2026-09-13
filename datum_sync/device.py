"""OAuth device authorisation (RFC 8628): elevation for an agent with no browser.

spec/agent-auth-plane/04 §3. The agent asks with its baseline token (so the
request is for *its* principal and nobody else's), a person approves on the
Approvals screen -- or policy auto-approves a scope that unlocks nothing --
and the agent polls `/oauth/token` with `grant_type=...device_code` until
the tokens are minted. The tokens are ordinary `oauth_tokens` rows bound to
the agent's principal with the approved scope; `auth.scope_tier` then caps
what they may do exactly as a consent-page grant would.

Who may approve: tier 4, or the requesting principal's sponsor when the
requested ceiling is within the sponsor's own effective tier. An approver
may narrow the scope, never widen it (ELEV-003).

`elevate()` is what the MCP built-in of the same name calls: the same
request, made for the gateway's own registered client so the agent does
not have to register one.
"""
from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

import asyncpg
from fastapi import APIRouter, Body, Depends, Request
from fastapi.responses import JSONResponse

from datum_sync import audit, auth, config, db, principals
from datum_sync.auth import Principal
from datum_sync.errors import ApiError

router = APIRouter()

DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"

# No 0/O, 1/I/L: a code is read out loud.
_USER_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"

# The gateway's own public client, for the `elevate` built-in.
ELEVATE_CLIENT_ID = "datum-sync-elevate"


def _caller(request: Request) -> Principal:
    return request.state.principal


Caller = Depends(_caller)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _user_code() -> str:
    raw = "".join(secrets.choice(_USER_CODE_ALPHABET) for _ in range(8))
    return f"{raw[:4]}-{raw[4:]}"


def validate_scope(scope: str | None) -> str:
    """The scope string, or `mcp` when absent; unknown tokens are refused."""
    from datum_sync.oauth import OAuthError

    if not scope or not scope.strip():
        return "mcp"
    parts = scope.split()
    unknown = [s for s in parts if s not in auth.SCOPE_TIERS]
    if unknown:
        raise OAuthError("invalid_scope", f"unknown scope: {' '.join(unknown)}")
    return " ".join(dict.fromkeys(parts))


def clamp_scope(scope: str, ceiling: int) -> str:
    """Drop every scope above `ceiling`. Never empty: `mcp` is the floor."""
    kept = [s for s in scope.split() if auth.SCOPE_TIERS.get(s, 0) <= ceiling]
    return " ".join(kept) or "mcp"


def _public(row: asyncpg.Record) -> dict[str, Any]:
    def when(v):
        return v.isoformat() if v else None

    status = "pending"
    if row["consumed_at"]:
        status = "consumed"
    elif row["decision"]:
        status = row["decision"]
    elif row["expires_at"] < _now():
        status = "expired"
    return {
        "id": row["id"],
        "user_code": row["user_code"],
        "principal": row["principal_name"],
        "principal_kind": row["principal_kind"],
        "sponsor": row["sponsor"],
        "client_id": row["client_id"],
        "client_name": row["client_name"],
        "scope": row["scope"],
        "approved_scope": row["approved_scope"],
        "requested_tier": auth.scope_tier(row["scope"]),
        "principal_max_tier": row["principal_max_tier"],
        "unlocks": {s: f"tier {t}: {auth.TIER_VERBS[t]}" for s, t in auth.SCOPE_TIERS.items()
                    if s in row["scope"].split() and t <= row["principal_max_tier"]},
        "status": status,
        "requested_at": when(row["requested_at"]),
        "expires_at": when(row["expires_at"]),
        "decided_by": row["decided_name"],
        "decided_at": when(row["decided_at"]),
        "reason": row["reason"],
    }


_COLS = """
    d.*, sa.name AS principal_name, sa.kind AS principal_kind, sa.max_tier AS principal_max_tier,
    sa.parent_id AS principal_parent_id,
    (SELECT p.name FROM service_accounts p WHERE p.id = sa.parent_id) AS sponsor,
    c.client_name
"""


async def _load(conn: asyncpg.Connection, elevation_id: int) -> asyncpg.Record:
    row = await conn.fetchrow(
        f"""
        SELECT {_COLS} FROM oauth_device_codes d
          JOIN service_accounts sa ON sa.id = d.principal_id
          JOIN oauth_clients c ON c.client_id = d.client_id
         WHERE d.id = $1
        """,
        elevation_id,
    )
    if row is None:
        raise ApiError(404, "NOT_FOUND", f"no elevation request {elevation_id}")
    return row


# -- the request -------------------------------------------------------------------


async def request_device_code(
    conn: asyncpg.Connection, principal: Principal, client_id: str, scope: str, resource: str | None
) -> dict[str, Any]:
    """Create the request; returns the RFC 8628 response body."""
    from datum_sync import oauth

    client = await oauth._client(conn, client_id)
    audience = oauth._check_resource(resource)
    scope = validate_scope(scope)
    if auth.scope_tier(scope) > principal.max_tier:
        raise oauth.OAuthError(
            "invalid_scope",
            f"{principal.name} is tier {principal.max_tier}; the scope asks for more",
        )
    device = auth.new_token()
    auto = all(s in config.POLICY_ELEVATION_AUTO_APPROVE_SCOPES for s in scope.split())
    row = await conn.fetchrow(
        """
        INSERT INTO oauth_device_codes
            (device_code_hash, user_code, client_id, principal_id, scope, resource,
             interval_seconds, expires_at, decision, approved_scope, decided_name, decided_at)
        VALUES ($1, $2, $3, $4, $5, $6, $7, now() + make_interval(secs => $8),
                CASE WHEN $9 THEN 'approved' END, CASE WHEN $9 THEN $5 END,
                CASE WHEN $9 THEN 'policy' END, CASE WHEN $9 THEN now() END)
        RETURNING id, user_code, expires_at
        """,
        auth.hash_token(device), _user_code(), client["client_id"], principal.account_id,
        scope, audience, config.DEVICE_POLL_INTERVAL_SECONDS, config.DEVICE_CODE_TTL_SECONDS, auto,
    )
    verification = f"{config.PUBLIC_URL}/ui/v2#/approvals"
    return {
        "device_code": device,
        "user_code": row["user_code"],
        "verification_uri": verification,
        "verification_uri_complete": f"{verification}?code={row['user_code']}",
        "expires_in": config.DEVICE_CODE_TTL_SECONDS,
        "interval": config.DEVICE_POLL_INTERVAL_SECONDS,
        "elevation_id": row["id"],
        "auto_approved": auto,
    }


@router.post("/oauth/device")
async def device_authorization(request: Request, caller: Principal = Caller) -> JSONResponse:
    """RFC 8628 §3.1, form- or JSON-encoded. Bearer required (ELEV-005)."""
    from datum_sync.oauth import OAuthError

    ctype = request.headers.get("content-type", "")
    if "json" in ctype:
        body = await request.json()
    else:
        form = await request.form()
        body = dict(form)
    client_id = body.get("client_id")
    if not client_id:
        raise OAuthError("invalid_request", "client_id is required")
    async with db.pool().acquire() as conn:
        if client_id == ELEVATE_CLIENT_ID:
            await ensure_elevate_client(conn)
        out = await request_device_code(conn, caller, str(client_id), body.get("scope"), body.get("resource"))
        trace = getattr(request.state, "trace", None) or audit.Trace.mint(None)
        await audit.write(
            conn, trace=trace, principal=caller, via="oauth", verb="oauth.device.request",
            target_kind="principal", target=caller.name, outcome="ok",
            detail={"scope": body.get("scope") or "mcp", "auto": out["auto_approved"],
                    "elevation_id": out["elevation_id"]},
        )
    out.pop("elevation_id")
    return JSONResponse(content=out, headers={"Cache-Control": "no-store"})


# -- the poll ------------------------------------------------------------------------


async def exchange_device_code(
    conn: asyncpg.Connection, client: asyncpg.Record, device_code: str | None
) -> dict[str, Any]:
    """`grant_type=device_code` on /oauth/token. RFC 8628 §3.4-3.5 errors."""
    from datum_sync import oauth

    if not device_code:
        raise oauth.OAuthError("invalid_request", "device_code is required")
    # Errors are collected, not raised, inside the transaction: a pending
    # answer is an error on the wire, and raising it would roll back the fact
    # that the client polled, so the interval could never be enforced.
    error: Exception | None = None
    body: dict[str, Any] | None = None
    async with conn.transaction():
        row = await conn.fetchrow(
            "SELECT * FROM oauth_device_codes WHERE device_code_hash = $1 FOR UPDATE",
            auth.hash_token(device_code),
        )
        if row is None or row["client_id"] != client["client_id"]:
            raise oauth.OAuthError("invalid_grant", "unknown device code")
        if row["consumed_at"] is not None:
            # Presented twice: the signature of a copied credential. Same
            # answer as a replayed authorization code (ELEV-006).
            error = oauth._Reuse(row["client_id"], row["principal_id"], "device code already used")
        elif row["expires_at"] < _now():
            error = oauth.OAuthError("expired_token", "the device code has expired")
        else:
            last = row["last_polled_at"]
            interval = row["interval_seconds"]
            await conn.execute(
                "UPDATE oauth_device_codes SET last_polled_at = now() WHERE id = $1", row["id"]
            )
            if last is not None and (_now() - last).total_seconds() < interval:
                # Backing off is asked for and enforced: the interval grows (ELEV-008).
                await conn.execute(
                    "UPDATE oauth_device_codes SET interval_seconds = interval_seconds + 5 WHERE id = $1",
                    row["id"],
                )
                error = oauth.OAuthError("slow_down", f"poll no faster than every {interval + 5} seconds")
            elif row["decision"] is None:
                error = oauth.OAuthError("authorization_pending", "not approved yet")
            elif row["decision"] == "denied":
                error = oauth.OAuthError("access_denied", row["reason"] or "the request was denied")
            else:
                account = await conn.fetchrow(
                    "SELECT id, state FROM service_accounts WHERE id = $1", row["principal_id"]
                )
                if account is None or account["state"] != "active":
                    error = oauth.OAuthError("invalid_grant", "the principal is no longer active")
                else:
                    await conn.execute(
                        "UPDATE oauth_device_codes SET consumed_at = now() WHERE id = $1", row["id"]
                    )
                    body = await oauth._mint(
                        conn, client["client_id"], row["principal_id"], row["resource"],
                        row["approved_scope"],
                    )
    if error is not None:
        raise error
    assert body is not None
    return body


# -- approvals (REST) ------------------------------------------------------------------


def _may_decide(caller: Principal, row: asyncpg.Record) -> None:
    if caller.effective_tier >= 4:
        return
    if row["principal_parent_id"] == caller.account_id and caller.effective_tier >= 3 \
            and auth.scope_tier(row["scope"]) <= caller.effective_tier:
        return
    raise ApiError(
        403, "FORBIDDEN",
        "approving an elevation needs tier 4, or the principal's sponsor within its own tier",
    )


@router.get("/rest/v1/elevations")
async def list_elevations(request: Request, caller: Principal = Caller) -> dict[str, Any]:
    auth.require_tier(caller, 3, "listing elevation requests")
    status = request.query_params.get("status", "pending")
    where = ["true"]
    args: list[Any] = []
    if status == "pending":
        where.append("d.decision IS NULL AND d.expires_at > now()")
    elif status in ("approved", "denied"):
        args.append(status)
        where.append(f"d.decision = ${len(args)}")
    if caller.effective_tier < 4:
        args.append(caller.account_id)
        where.append(f"(sa.parent_id = ${len(args)} OR sa.id = ${len(args)})")
    async with db.pool().acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT {_COLS} FROM oauth_device_codes d
              JOIN service_accounts sa ON sa.id = d.principal_id
              JOIN oauth_clients c ON c.client_id = d.client_id
             WHERE {' AND '.join(where)}
             ORDER BY d.requested_at DESC LIMIT 200
            """,
            *args,
        )
    return {"items": [_public(r) for r in rows]}


async def _decide(
    request: Request, caller: Principal, elevation_id: int, decision: str,
    scope: str | None, reason: str | None,
) -> dict[str, Any]:
    async with db.pool().acquire() as conn:
        row = await _load(conn, elevation_id)
        _may_decide(caller, row)
        if row["decision"] is not None:
            raise ApiError(409, "ALREADY_DECIDED", f"already {row['decision']}")
        if row["expires_at"] < _now():
            raise ApiError(409, "EXPIRED", "the request has expired; the agent must ask again")
        approved = None
        if decision == "approved":
            approved = validate_scope(scope) if scope else row["scope"]
            asked = set(row["scope"].split())
            if not set(approved.split()) <= asked or auth.scope_tier(approved) > auth.scope_tier(row["scope"]):
                # Narrow, never widen (ELEV-003).
                raise ApiError(400, "SCOPE_WIDER_THAN_REQUESTED",
                               "an approval may narrow the requested scope, never widen it",
                               {"requested": row["scope"], "approved": approved})
        await conn.execute(
            """
            UPDATE oauth_device_codes
               SET decision = $2, approved_scope = $3, decided_by = $4, decided_name = $5,
                   decided_at = now(), reason = $6
             WHERE id = $1
            """,
            elevation_id, decision, approved, caller.account_id, caller.name, reason,
        )
        trace = getattr(request.state, "trace", None) or audit.Trace.mint(None)
        await audit.write(
            conn, trace=trace, principal=caller, via="rest",
            verb=f"oauth.device.{'approve' if decision == 'approved' else 'deny'}",
            target_kind="principal", target=row["principal_name"],
            outcome="ok" if decision == "approved" else "denied", governance=True,
            detail={"elevation_id": elevation_id, "scope": row["scope"], "approved_scope": approved,
                    "reason": reason},
        )
        row = await _load(conn, elevation_id)
    return _public(row)


@router.post("/rest/v1/elevations/{elevation_id}/approve")
async def approve_elevation(
    elevation_id: int, request: Request, body: dict[str, Any] = Body(default_factory=dict),
    caller: Principal = Caller,
) -> dict[str, Any]:
    auth.require_tier(caller, 3, "approving an elevation")
    return await _decide(request, caller, elevation_id, "approved", body.get("scope"), None)


@router.post("/rest/v1/elevations/{elevation_id}/deny")
async def deny_elevation(
    elevation_id: int, request: Request, body: dict[str, Any] = Body(default_factory=dict),
    caller: Principal = Caller,
) -> dict[str, Any]:
    auth.require_tier(caller, 3, "denying an elevation")
    reason = body.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        raise ApiError(400, "INVALID_PARAMETER", "reason is required", {"field": "reason"})
    return await _decide(request, caller, elevation_id, "denied", None, reason.strip())


# -- the MCP built-in ----------------------------------------------------------------


async def ensure_elevate_client(conn: asyncpg.Connection) -> str:
    """The gateway's own public client, created on first use."""
    await conn.execute(
        """
        INSERT INTO oauth_clients (client_id, client_name, secret_hash, redirect_uris, grant_types)
        VALUES ($1, 'Datum-Sync elevate', NULL, ARRAY[$2], ARRAY['authorization_code', 'refresh_token'])
        ON CONFLICT (client_id) DO NOTHING
        """,
        ELEVATE_CLIENT_ID, f"{config.PUBLIC_URL}/ui/v2",
    )
    return ELEVATE_CLIENT_ID


async def elevate(conn: asyncpg.Connection, principal: Principal, scope: str) -> dict[str, Any]:
    client_id = await ensure_elevate_client(conn)
    return await request_device_code(conn, principal, client_id, scope, None)
