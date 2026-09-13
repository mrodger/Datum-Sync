"""Principals over HTTP: list, create, read, edit, delete, and their tokens.

spec/agent-auth-plane/03 §1-§2 and 07 §1. The lifecycle verbs (approve,
restrict, restore, retire, review) and enrolment are WP2 and live in
lifecycle.py; this module is the authority half.

Every write goes through two checks, in this order:

  1. `_may_edit` -- may THIS caller set THAT grant on THIS target (03 §2's
     table: tier 5 anything; tier 4 within its own grant, tier <= 4; tier 3
     its own children, within its own grant; below that nothing).
  2. `grants.narrows` against the parent (PRIN-001) and, on an edit, every
     descendant against the new grant (PRIN-005) -- refused with the field
     names, because "grant not narrower" sends an operator to the wrong box.

`/rest/v1/accounts/*` in api.py stays for one release as the read-mostly
view it always was; nothing here is reachable through it.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any

import asyncpg
from fastapi import APIRouter, Body, Depends, Request

from datum_sync import audit, auth, config, db, grants, tokens, vault
from datum_sync.auth import Principal
from datum_sync.errors import ApiError

router = APIRouter()


def _caller(request: Request) -> Principal:
    return request.state.principal


Caller = Depends(_caller)

# PRINCIPAL_COLS plus the descriptive columns the screens show.
_COLS = f"""
    {auth.PRINCIPAL_COLS},
    sa.description, sa.metadata, sa.created_at, sa.last_used_at, sa.created_by,
    sa.review_due_at, sa.last_reviewed_at, sa.last_reviewed_by, sa.restricted_reason,
    sa.password_hash IS NOT NULL AS has_password,
    EXISTS (SELECT 1 FROM account_tokens t
             WHERE t.account_id = sa.id AND t.revoked_at IS NULL
               AND (t.expires_at IS NULL OR t.expires_at > now())) AS has_token,
    (SELECT count(*) FROM service_accounts c WHERE c.parent_id = sa.id) AS children_count,
    (SELECT p.name FROM service_accounts p WHERE p.id = sa.parent_id) AS parent
"""

AUTHORITY_FIELDS = (
    "max_tier", "repo_scope", "vault_scope", "proxy_grants",
    "federation_scope", "limits", "rate_limit_per_min",
)
_LIMIT_KEYS = ("concurrent_sessions", "session_idle_seconds", "jobs_per_hour", "concurrent_jobs")
_FED_KINDS = ("code", "compute", "documents", "mcp")


# -- loading -----------------------------------------------------------------


async def _load(conn: asyncpg.Connection, name: str) -> asyncpg.Record:
    row = await conn.fetchrow(
        f"SELECT {_COLS} FROM service_accounts sa WHERE sa.name = $1", name
    )
    if row is None:
        raise ApiError(404, "NOT_FOUND", f"no such principal: {name}")
    return row


async def _ancestors(conn: asyncpg.Connection, account_id: int) -> list[asyncpg.Record]:
    return await conn.fetch(auth._ANCESTORS_SQL, account_id)


async def effective_authority(conn: asyncpg.Connection, row: asyncpg.Record) -> dict:
    """The meet of a row's own authority and its ancestors', state ignored.

    `auth.effective` is for a request and raises on a non-active state; this
    is for an operator looking at or editing a row in any state.
    """
    out = auth._row_authority(row)
    if row["state"] == "restricted":
        out = grants.restricted(out)
    for anc in await _ancestors(conn, row["account_id"]):
        out = grants.intersect(out, auth._row_authority(anc))
    return out


async def _descendants(conn: asyncpg.Connection, account_id: int) -> list[asyncpg.Record]:
    return await conn.fetch(
        """
        WITH RECURSIVE tree AS (
            SELECT sa.id, sa.parent_id, sa.name, sa.max_tier, sa.repo_scope,
                   sa.vault_scope, sa.proxy_grants, sa.federation_scope, sa.limits,
                   sa.rate_limit_per_min, 1 AS depth
              FROM service_accounts sa WHERE sa.parent_id = $1
            UNION ALL
            SELECT c.id, c.parent_id, c.name, c.max_tier, c.repo_scope,
                   c.vault_scope, c.proxy_grants, c.federation_scope, c.limits,
                   c.rate_limit_per_min, tree.depth + 1
              FROM service_accounts c JOIN tree ON c.parent_id = tree.id
             WHERE tree.depth < 16
        )
        SELECT * FROM tree ORDER BY depth, name
        """,
        account_id,
    )


def public(row: asyncpg.Record, effective: dict | None = None) -> dict[str, Any]:
    def when(v: datetime | None) -> str | None:
        return v.isoformat() if v else None

    out = {
        "name": row["name"],
        "kind": row["kind"],
        "state": row["state"],
        "parent": row["parent"],
        "description": row["description"],
        "max_tier": row["max_tier"],
        "is_admin": row["is_admin"],
        "disabled": row["disabled"],
        "repo_scope": list(row["repo_scope"]),
        "vault_scope": auth.json_of(row, "vault_scope"),
        "proxy_grants": list(row["proxy_grants"] or []),
        "federation_scope": auth.json_of(row, "federation_scope"),
        "limits": auth.json_of(row, "limits") or {},
        "rate_limit_per_min": row["rate_limit_per_min"],
        "metadata": auth.json_of(row, "metadata") or {},
        "has_token": row["has_token"],
        "has_password": row["has_password"],
        "children_count": row["children_count"],
        "created_at": when(row["created_at"]),
        "created_by": row["created_by"],
        "last_used_at": when(row["last_used_at"]),
        "review_due_at": when(row["review_due_at"]),
        "last_reviewed_at": when(row["last_reviewed_at"]),
        "last_reviewed_by": row["last_reviewed_by"],
        "restricted_reason": row["restricted_reason"],
    }
    if effective is not None:
        out["effective"] = effective
        out["effective_tier"] = effective["max_tier"]
    return out


# -- validation --------------------------------------------------------------


def _bad(msg: str, field: str | None = None) -> ApiError:
    return ApiError(400, "INVALID_PARAMETER", msg, {"field": field} if field else None)


def validate_authority(body: dict[str, Any], *, partial: bool) -> dict[str, Any]:
    """The authority fields of a request body, checked for shape.

    Shape only: whether the values are *permitted* is `_may_edit` and
    `grants.narrows`, which need the database. Returns only the fields that
    were present when `partial`, so a PATCH does not reset what it did not
    name.
    """
    out: dict[str, Any] = {}
    if "max_tier" in body or not partial:
        tier = body.get("max_tier", 1)
        if not isinstance(tier, int) or isinstance(tier, bool) or not 1 <= tier <= 5:
            raise _bad("max_tier must be an integer 1-5", "max_tier")
        out["max_tier"] = tier
    if "repo_scope" in body or not partial:
        scope = body.get("repo_scope", [])
        if not isinstance(scope, list) or not all(isinstance(x, str) and x for x in scope):
            raise _bad("repo_scope must be a list of repository names", "repo_scope")
        if "*" in scope and len(scope) > 1:
            raise _bad("repo_scope is either ['*'] or a list of names", "repo_scope")
        out["repo_scope"] = scope
    if "vault_scope" in body:
        vs = body["vault_scope"]
        if vs is not None:
            if not isinstance(vs, dict):
                raise _bad("vault_scope must be an object or null", "vault_scope")
            try:
                vault.validate_scope(vs)
            except vault.VaultScopeError as exc:
                raise _bad(f"vault_scope: {exc}", "vault_scope") from None
        out["vault_scope"] = vs
    elif not partial:
        out["vault_scope"] = None
    if "proxy_grants" in body or not partial:
        pg = body.get("proxy_grants", [])
        if not isinstance(pg, list) or not all(isinstance(x, str) and x for x in pg):
            raise _bad("proxy_grants must be a list of connection names", "proxy_grants")
        if "*" in pg:
            raise _bad("proxy_grants are enumerated; '*' is not allowed", "proxy_grants")
        out["proxy_grants"] = pg
    if "federation_scope" in body:
        fs = body["federation_scope"]
        if fs is not None:
            if not isinstance(fs, dict) or not all(
                k in _FED_KINDS and isinstance(v, dict) for k, v in fs.items()
            ):
                raise _bad(
                    f"federation_scope keys must be among {', '.join(_FED_KINDS)}, "
                    "each an object", "federation_scope",
                )
        out["federation_scope"] = fs
    elif not partial:
        out["federation_scope"] = None
    if "limits" in body or not partial:
        limits = body.get("limits", {}) or {}
        if not isinstance(limits, dict):
            raise _bad("limits must be an object", "limits")
        for k, v in limits.items():
            if k not in _LIMIT_KEYS:
                raise _bad(f"unknown limit {k!r}; known: {', '.join(_LIMIT_KEYS)}", "limits")
            if v is not None and (not isinstance(v, int) or isinstance(v, bool) or v < 0):
                raise _bad(f"limits.{k} must be a non-negative integer or null", "limits")
        out["limits"] = limits
    if "rate_limit_per_min" in body:
        rl = body["rate_limit_per_min"]
        if rl is not None and (not isinstance(rl, int) or isinstance(rl, bool) or rl < 1):
            raise _bad("rate_limit_per_min must be a positive integer or null", "rate_limit_per_min")
        out["rate_limit_per_min"] = rl
    elif not partial:
        out["rate_limit_per_min"] = None
    return out


# -- who may edit ------------------------------------------------------------


def _may_edit(
    actor: Principal, target_effective: dict | None, new: dict, parent_id: int | None
) -> None:
    """03 §2's table. Raises 403 naming the rule that refused."""
    tier = actor.effective_tier
    if tier >= 5:
        return
    if tier >= 4:
        own = actor.authority()
        if target_effective is not None and grants.narrows(target_effective, own):
            raise ApiError(
                403, "FORBIDDEN",
                "a tier-4 administrator may only edit principals within their own grant",
                {"wider_in": grants.narrows(target_effective, own)},
            )
        wider = grants.narrows(new, own)
        if wider:
            raise ApiError(
                403, "FORBIDDEN",
                "a tier-4 administrator may only grant what they hold",
                {"wider_in": wider},
            )
        if new.get("max_tier", 1) > 4:
            raise ApiError(403, "FORBIDDEN", "only tier 5 may set tier 5")
        return
    if tier >= 3:
        if parent_id != actor.account_id:
            raise ApiError(
                403, "FORBIDDEN", "a tier-3 principal may only edit its own children"
            )
        wider = grants.narrows(new, actor.authority())
        if wider:
            raise ApiError(
                403, "FORBIDDEN", "a tier-3 sponsor may only grant what it holds",
                {"wider_in": wider},
            )
        if new.get("max_tier", 1) > 2:
            raise ApiError(403, "FORBIDDEN", "a tier-3 sponsor may create tier <= 2 only")
        return
    raise ApiError(403, "TIER_REQUIRED", "editing principals requires tier 3",
                   {"required": 3, "effective": tier})


async def _check_parent(
    conn: asyncpg.Connection, parent_row: asyncpg.Record, new: dict
) -> None:
    """PRIN-001 and PRIN-012 against a parent row."""
    parent_effective = await effective_authority(conn, parent_row)
    wider = grants.narrows(new, parent_effective)
    if wider:
        raise ApiError(
            400, "GRANT_NOT_NARROWER",
            f"grant is wider than {parent_row['name']!r}'s in: {', '.join(wider)}",
            {"fields": wider, "parent": parent_row["name"]},
        )
    depth = len(await _ancestors(conn, parent_row["account_id"])) + 1
    if depth >= config.POLICY_MAX_DELEGATION_DEPTH:
        raise ApiError(
            400, "DELEGATION_TOO_DEEP",
            f"a child of {parent_row['name']!r} would be at depth {depth + 1}; "
            f"the limit is {config.POLICY_MAX_DELEGATION_DEPTH}",
        )


def _authority_sql(values: dict) -> tuple[list[str], list[Any]]:
    sets, args = [], []
    for key in AUTHORITY_FIELDS:
        if key not in values:
            continue
        v = values[key]
        if key in ("vault_scope", "federation_scope", "limits"):
            v = json.dumps(v) if v is not None else None
        args.append(v)
        sets.append(f"{key} = ${len(args) + 1}")
    return sets, args


async def _audit(request: Request, caller: Principal, verb: str, target: str, detail: dict | None = None):
    trace = getattr(request.state, "trace", None) or audit.Trace.mint(None)
    async with db.pool().acquire() as conn:
        await audit.write(
            conn, trace=trace, principal=caller, via="rest", verb=verb,
            target_kind="principal", target=target, outcome="ok",
            governance=True, detail=detail,
        )


# -- routes ------------------------------------------------------------------


@router.get("/rest/v1/principals")
async def list_principals(request: Request, caller: Principal = Caller) -> dict[str, Any]:
    """Tier 4 sees every principal; tier 3 sees itself and its subtree."""
    auth.require_tier(caller, 3, "listing principals")
    q = request.query_params
    where, args = ["true"], []
    for key in ("kind", "state"):
        if q.get(key):
            args.append(q[key])
            where.append(f"sa.{key} = ${len(args)}")
    if q.get("parent"):
        args.append(q["parent"])
        where.append(f"sa.parent_id = (SELECT id FROM service_accounts WHERE name = ${len(args)})")
    if q.get("q"):
        args.append(f"%{q['q']}%")
        where.append(f"sa.name ILIKE ${len(args)}")
    if caller.effective_tier < 4:
        args.append(caller.account_id)
        where.append(f"(sa.id = ${len(args)} OR sa.parent_id = ${len(args)})")
    async with db.pool().acquire() as conn:
        rows = await conn.fetch(
            f"SELECT {_COLS} FROM service_accounts sa WHERE {' AND '.join(where)} "
            "ORDER BY sa.kind, sa.name",
            *args,
        )
    return {"items": [public(r) for r in rows]}


@router.post("/rest/v1/principals", status_code=201)
async def create_principal(
    request: Request, body: dict[str, Any] = Body(...), caller: Principal = Caller
) -> dict[str, Any]:
    auth.require_tier(caller, 3, "creating a principal")
    name = body.get("name")
    if not isinstance(name, str) or not name.strip():
        raise _bad("name is required", "name")
    name = name.strip()
    kind = body.get("kind", "agent" if caller.effective_tier < 4 else "human")
    if kind not in ("human", "agent", "system"):
        raise _bad("kind must be human, agent or system", "kind")
    new = validate_authority(body, partial=False)
    if kind == "agent" and new["max_tier"] > 3:
        raise _bad("an agent is at most tier 3", "max_tier")
    metadata = body.get("metadata") or {}
    if not isinstance(metadata, dict):
        raise _bad("metadata must be an object", "metadata")
    description = body.get("description")
    parent_name = body.get("parent")
    if parent_name is None and caller.effective_tier < 4:
        parent_name = caller.name
    if kind == "agent" and parent_name is None:
        raise _bad("an agent needs a parent (its sponsor)", "parent")

    async with db.pool().acquire() as conn:
        parent_row = await _load(conn, parent_name) if parent_name else None
        parent_id = parent_row["account_id"] if parent_row else None
        # The structural rule first (PRIN-001): when the parent is the caller,
        # both checks would refuse, and "wider than your own grant" is the
        # less useful of the two messages.
        if parent_row is not None:
            await _check_parent(conn, parent_row, new)
        _may_edit(caller, None, new, parent_id)
        sets, args = _authority_sql(new)
        try:
            row_id = await conn.fetchval(
                f"""
                INSERT INTO service_accounts
                    (name, kind, parent_id, description, metadata, created_by,
                     {', '.join(k for k in AUTHORITY_FIELDS if k in new)})
                VALUES ($1, $2, $3, $4, $5, $6,
                        {', '.join(f'${i + 7}' for i in range(len(args)))})
                RETURNING id
                """,
                name, kind, parent_id, description, json.dumps(metadata), caller.name, *args,
            )
        except asyncpg.UniqueViolationError:
            raise ApiError(409, "ALREADY_EXISTS", f"a principal named {name!r} exists") from None
        row = await _load(conn, name)
        eff = await effective_authority(conn, row)
    await _audit(request, caller, "principal.create", name, {"kind": kind, "parent": parent_name})
    return public(row, eff)


@router.get("/rest/v1/principals/{name}")
async def get_principal(name: str, caller: Principal = Caller) -> dict[str, Any]:
    async with db.pool().acquire() as conn:
        row = await _load(conn, name)
        _require_visible(caller, row)
        eff = await effective_authority(conn, row)
        ancestors = [a["name"] for a in await _ancestors(conn, row["account_id"])]
        children = [
            c["name"] for c in await conn.fetch(
                "SELECT name FROM service_accounts WHERE parent_id = $1 ORDER BY name",
                row["account_id"],
            )
        ]
        toks = await tokens.list_for_account(conn, row["account_id"])
    out = public(row, eff)
    out["ancestors"] = ancestors
    out["children"] = children
    out["tokens"] = [tokens.public(t) for t in toks]
    return out


def _require_visible(caller: Principal, row: asyncpg.Record) -> None:
    """Self, sponsor, or tier 4."""
    if caller.effective_tier >= 4:
        return
    if row["account_id"] == caller.account_id or row["parent_id"] == caller.account_id:
        return
    raise ApiError(403, "FORBIDDEN", "not your principal")


@router.patch("/rest/v1/principals/{name}")
async def update_principal(
    name: str, request: Request, body: dict[str, Any] = Body(...), caller: Principal = Caller
) -> dict[str, Any]:
    for forbidden in ("is_admin", "state", "disabled", "kind", "parent", "name"):
        if forbidden in body:
            # `is_admin` is derived from the tier (PRIN-004); the others have
            # their own verbs so the audit trail says what happened.
            raise _bad(f"{forbidden} cannot be set here", forbidden)
    changes = validate_authority(body, partial=True)
    descriptive: dict[str, Any] = {}
    if "metadata" in body:
        if not isinstance(body["metadata"], dict):
            raise _bad("metadata must be an object", "metadata")
        descriptive["metadata"] = json.dumps(body["metadata"])
    if "description" in body:
        descriptive["description"] = body["description"]
    if not changes and not descriptive:
        raise _bad("nothing to change")

    async with db.pool().acquire() as conn:
        row = await _load(conn, name)
        target_eff = await effective_authority(conn, row)
        new = {**auth._row_authority(row), **changes}
        if row["kind"] == "agent" and new["max_tier"] > 3:
            raise _bad("an agent is at most tier 3", "max_tier")
        if changes:
            if row["parent_id"] is not None:
                parent_row = await _load(conn, row["parent"])
                await _check_parent(conn, parent_row, new)
            _may_edit(caller, target_eff, new, row["parent_id"])
            # PRIN-005: no descendant may be left wider than the new grant.
            widened = []
            for d in await _descendants(conn, row["account_id"]):
                if d["parent_id"] == row["account_id"] and grants.narrows(auth._row_authority(d), new):
                    widened.append(d["name"])
            if widened:
                raise ApiError(
                    409, "DESCENDANTS_WOULD_WIDEN",
                    "these children would no longer narrow this grant: " + ", ".join(widened),
                    {"names": widened},
                )
        elif caller.effective_tier < 4:
            _require_visible(caller, row)
        sets, args = _authority_sql(changes)
        for k, v in descriptive.items():
            args.append(v)
            sets.append(f"{k} = ${len(args) + 1}")
        await conn.execute(
            f"UPDATE service_accounts SET {', '.join(sets)} WHERE id = $1",
            row["account_id"], *args,
        )
        row = await _load(conn, name)
        eff = await effective_authority(conn, row)
    await _audit(request, caller, "principal.update", name, {"fields": sorted(changes) + sorted(descriptive)})
    return public(row, eff)


@router.post("/rest/v1/principals/{name}/disable")
async def disable_principal(name: str, request: Request, caller: Principal = Caller) -> dict[str, Any]:
    return await _set_disabled(name, request, caller, True)


@router.post("/rest/v1/principals/{name}/enable")
async def enable_principal(name: str, request: Request, caller: Principal = Caller) -> dict[str, Any]:
    return await _set_disabled(name, request, caller, False)


async def _set_disabled(name: str, request: Request, caller: Principal, value: bool) -> dict[str, Any]:
    auth.require_tier(caller, 4, "disabling a principal")
    async with db.pool().acquire() as conn:
        row = await _load(conn, name)
        if value and row["account_id"] == caller.account_id:
            raise _bad("cannot disable your own principal")
        await conn.execute(
            "UPDATE service_accounts SET disabled = $2 WHERE id = $1", row["account_id"], value
        )
        row = await _load(conn, name)
    await _audit(request, caller, "principal.disable" if value else "principal.enable", name)
    return public(row)


@router.delete("/rest/v1/principals/{name}")
async def delete_principal(name: str, request: Request, caller: Principal = Caller) -> dict[str, Any]:
    """Tier 5. Children go first: RESTRICT on parent_id makes this loud."""
    auth.require_tier(caller, 5, "deleting a principal")
    async with db.pool().acquire() as conn:
        row = await _load(conn, name)
        if row["account_id"] == caller.account_id:
            raise _bad("cannot delete your own principal")
        if row["children_count"]:
            raise ApiError(409, "CHILDREN_ACTIVE", "delete or re-parent the children first",
                           {"children": row["children_count"]})
        await conn.execute("DELETE FROM service_accounts WHERE id = $1", row["account_id"])
    await _audit(request, caller, "principal.delete", name)
    return {"principal": name, "deleted": True}


# -- tokens --------------------------------------------------------------------


def _require_token_access(caller: Principal, row: asyncpg.Record) -> None:
    """Self at tier >= 3, sponsor, or tier 4."""
    if caller.effective_tier >= 4:
        return
    if row["parent_id"] == caller.account_id and caller.effective_tier >= 3:
        return
    if row["account_id"] == caller.account_id and caller.effective_tier >= 3:
        return
    raise ApiError(403, "FORBIDDEN", "not your principal")


@router.get("/rest/v1/principals/{name}/tokens")
async def list_tokens(name: str, caller: Principal = Caller) -> dict[str, Any]:
    async with db.pool().acquire() as conn:
        row = await _load(conn, name)
        _require_token_access(caller, row)
        rows = await tokens.list_for_account(conn, row["account_id"], include_revoked=True)
    return {"items": [tokens.public(t) for t in rows]}


@router.post("/rest/v1/principals/{name}/tokens", status_code=201)
async def mint_token(
    name: str, request: Request, body: dict[str, Any] = Body(...), caller: Principal = Caller
) -> dict[str, Any]:
    label = body.get("label")
    if not isinstance(label, str) or not label.strip():
        raise _bad("label is required", "label")
    cap = body.get("max_tier")
    if cap is not None and (not isinstance(cap, int) or isinstance(cap, bool) or not 1 <= cap <= 5):
        raise _bad("max_tier must be an integer 1-5", "max_tier")
    expires = body.get("expires_at")
    expires_at = None
    if expires is not None:
        try:
            expires_at = datetime.fromisoformat(str(expires))
        except ValueError:
            raise _bad("expires_at must be ISO-8601", "expires_at") from None
    async with db.pool().acquire() as conn:
        row = await _load(conn, name)
        _require_token_access(caller, row)
        try:
            trow, raw = await tokens.create(conn, row["account_id"], label.strip(), expires_at, cap)
        except tokens.TokenTierAbovePrincipal as exc:
            raise ApiError(400, "TOKEN_TIER_ABOVE_PRINCIPAL", str(exc)) from None
        except asyncpg.UniqueViolationError:
            raise ApiError(409, "ALREADY_EXISTS",
                           f"{name} already has a live token labelled {label!r}") from None
    await _audit(request, caller, "principal.token.mint", name, {"label": label, "max_tier": cap})
    return tokens.public(trow, token=raw)


@router.delete("/rest/v1/principals/{name}/tokens/{label}")
async def revoke_token(name: str, label: str, request: Request, caller: Principal = Caller) -> dict[str, Any]:
    async with db.pool().acquire() as conn:
        row = await _load(conn, name)
        _require_token_access(caller, row)
        done = await tokens.revoke(conn, row["account_id"], label)
    if not done:
        raise ApiError(404, "NOT_FOUND", f"{name} has no live token labelled {label!r}")
    await _audit(request, caller, "principal.token.revoke", name, {"label": label})
    return {"principal": name, "label": label, "revoked": True}
