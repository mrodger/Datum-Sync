"""MCP sessions: counted, grouped, superseded -- never a place for state.

spec/agent-auth-plane/03 §5 and 12 §2. `open()` is called by `initialize`,
`touch()` by every other MCP request, `close()` by `DELETE /mcp`, and
`end_for_*` by everything that withdraws a credential or a principal, so a
live session is not a way to outlive a revocation (SESS-003).

The limit is `limits.concurrent_sessions`, or a policy default that depends
on the credential: a capped token (the baseline of spec 02 §3) gets
POLICY_BASELINE_SESSIONS, anything else POLICY_ELEVATED_SESSIONS. At the
limit, a session on the SAME credential row supersedes the oldest such
session (a reconnecting client is the same conversation); on a different
credential the request is refused with the live sessions named.
"""
from __future__ import annotations

import uuid
from typing import Any

import asyncpg
from fastapi import APIRouter, Depends, Request

from datum_sync import audit, auth, config, db, principals
from datum_sync.auth import Principal
from datum_sync.errors import ApiError

router = APIRouter()


def _caller(request: Request) -> Principal:
    return request.state.principal


Caller = Depends(_caller)

SESSION_HEADER = "Mcp-Session-Id"


class SessionLimit(Exception):
    """Refused: every live session belongs to another credential."""

    def __init__(self, active: list[dict[str, Any]]) -> None:
        super().__init__("session limit reached")
        self.active = active


def limit_for(principal: Principal) -> int:
    value = (principal.limits or {}).get("concurrent_sessions")
    if value is not None:
        return int(value)
    if principal.token_tier_cap is not None:
        return config.POLICY_BASELINE_SESSIONS
    return config.POLICY_ELEVATED_SESSIONS


def idle_window(principal: Principal) -> int:
    value = (principal.limits or {}).get("session_idle_seconds")
    return int(value) if value is not None else config.SESSION_IDLE_SECONDS


def _public(row: asyncpg.Record) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "credential_kind": row["credential_kind"],
        "client_name": row["client_name"],
        "client_version": row["client_version"],
        "started_at": row["started_at"].isoformat(),
        "last_seen_at": row["last_seen_at"].isoformat(),
        "idle_seconds": int(row["idle_seconds"]) if row["idle_seconds"] is not None else None,
        "ended_at": row["ended_at"].isoformat() if row["ended_at"] else None,
        "end_reason": row["end_reason"],
    }


_LIVE_SQL = """
    SELECT s.*, extract(epoch FROM now() - s.last_seen_at) AS idle_seconds
      FROM mcp_sessions s
     WHERE s.principal_id = $1 AND s.ended_at IS NULL
       AND s.last_seen_at > now() - make_interval(secs => $2)
     ORDER BY s.last_seen_at
"""


async def live(conn: asyncpg.Connection, principal: Principal) -> list[asyncpg.Record]:
    return await conn.fetch(_LIVE_SQL, principal.account_id, idle_window(principal))


async def open(
    conn: asyncpg.Connection, principal: Principal, client: dict[str, Any] | None,
    protocol: str | None,
) -> tuple[uuid.UUID, str | None]:
    """Admit a session. Returns (id, superseded session id or None).

    Raises SessionLimit when every live session is another credential's.
    """
    limit = limit_for(principal)
    superseded = None
    async with conn.transaction():
        # Serialise admissions per principal: two simultaneous initializes
        # must not both count the same live set and both get in.
        await conn.execute("SELECT pg_advisory_xact_lock($1, $2)", 0x5E55, principal.account_id)
        rows = await live(conn, principal)
        if limit <= 0:
            raise SessionLimit([])
        if len(rows) >= limit:
            same = [
                r for r in rows
                if r["credential_kind"] == principal.source
                and r["token_id"] == principal.credential_id
            ]
            if not same:
                raise SessionLimit([{
                    "session_id": str(r["id"]),
                    "started_at": r["started_at"].isoformat(),
                    "idle_seconds": int(r["idle_seconds"]),
                    "client_name": r["client_name"],
                } for r in rows])
            oldest = same[0]
            await conn.execute(
                "UPDATE mcp_sessions SET ended_at = now(), end_reason = 'superseded' WHERE id = $1",
                oldest["id"],
            )
            superseded = str(oldest["id"])
        client = client or {}
        sid = await conn.fetchval(
            """
            INSERT INTO mcp_sessions
                (principal_id, credential_kind, token_id, client_name, client_version,
                 protocol_version)
            VALUES ($1, $2, $3, $4, $5, $6)
            RETURNING id
            """,
            principal.account_id, principal.source, principal.credential_id,
            str(client.get("name"))[:120] if client.get("name") else None,
            str(client.get("version"))[:60] if client.get("version") else None,
            protocol,
        )
    return sid, superseded


async def touch(conn: asyncpg.Connection, principal: Principal, raw: str) -> asyncpg.Record | None:
    """Validate a presented session id and bump it. None if it is not this
    principal's live session -- the caller answers 404 with no hint (SESS-002)."""
    try:
        sid = uuid.UUID(raw)
    except (ValueError, AttributeError):
        return None
    return await conn.fetchrow(
        """
        UPDATE mcp_sessions SET last_seen_at = now()
         WHERE id = $1 AND principal_id = $2 AND ended_at IS NULL
           AND last_seen_at > now() - make_interval(secs => $3)
        RETURNING id, credential_kind, token_id, client_name, started_at
        """,
        sid, principal.account_id, idle_window(principal),
    )


async def close(conn: asyncpg.Connection, principal: Principal, raw: str, reason: str = "client") -> bool:
    try:
        sid = uuid.UUID(raw)
    except (ValueError, AttributeError):
        return False
    done = await conn.fetchval(
        """
        UPDATE mcp_sessions SET ended_at = now(), end_reason = $3
         WHERE id = $1 AND principal_id = $2 AND ended_at IS NULL
        RETURNING id
        """,
        sid, principal.account_id, reason,
    )
    return done is not None


async def end_for_principal(conn: asyncpg.Connection, account_id: int, reason: str = "revoked") -> int:
    rows = await conn.fetch(
        """
        UPDATE mcp_sessions SET ended_at = now(), end_reason = $2
         WHERE principal_id = $1 AND ended_at IS NULL RETURNING id
        """,
        account_id, reason,
    )
    return len(rows)


async def end_for_credential(
    conn: asyncpg.Connection, kind: str, token_ids: list[int], reason: str = "revoked"
) -> int:
    if not token_ids:
        return 0
    rows = await conn.fetch(
        """
        UPDATE mcp_sessions SET ended_at = now(), end_reason = $3
         WHERE credential_kind = $1 AND token_id = ANY($2::bigint[]) AND ended_at IS NULL
        RETURNING id
        """,
        kind, token_ids, reason,
    )
    return len(rows)


async def sweep_idle(conn: asyncpg.Connection) -> int:
    """Stamp idle sessions for the record. Counting never depends on this."""
    rows = await conn.fetch(
        """
        UPDATE mcp_sessions SET ended_at = now(), end_reason = 'idle'
         WHERE ended_at IS NULL AND last_seen_at < now() - make_interval(secs => $1)
        RETURNING id
        """,
        config.SESSION_IDLE_SECONDS * 4,
    )
    return len(rows)


# -- REST --------------------------------------------------------------------------


@router.get("/rest/v1/principals/{name}/sessions")
async def list_sessions(name: str, caller: Principal = Caller) -> dict[str, Any]:
    async with db.pool().acquire() as conn:
        row = await principals._load(conn, name)
        principals._require_visible(caller, row)
        rows = await conn.fetch(
            """
            SELECT s.*, extract(epoch FROM now() - s.last_seen_at) AS idle_seconds
              FROM mcp_sessions s
             WHERE s.principal_id = $1
               AND (s.ended_at IS NULL OR s.ended_at > now() - interval '24 hours')
             ORDER BY s.ended_at IS NOT NULL, s.last_seen_at DESC
             LIMIT 200
            """,
            row["account_id"],
        )
        p = auth.principal_from_row(row, "local")
    return {
        "items": [_public(r) for r in rows],
        "limit": limit_for(p),
        "idle_seconds": idle_window(p),
    }


@router.delete("/rest/v1/principals/{name}/sessions/{session_id}")
async def end_session(name: str, session_id: str, request: Request, caller: Principal = Caller) -> dict[str, Any]:
    async with db.pool().acquire() as conn:
        row = await principals._load(conn, name)
        principals._require_visible(caller, row)
        target = auth.principal_from_row(row, "local")
        done = await close(conn, target, session_id, reason="revoked")
        if not done:
            raise ApiError(404, "NOT_FOUND", "no such live session")
        trace = getattr(request.state, "trace", None) or audit.Trace.mint(None)
        await audit.write(
            conn, trace=trace, principal=caller, via="rest", verb="session.end",
            target_kind="principal", target=name, outcome="ok", detail={"session_id": session_id},
        )
    return {"session_id": session_id, "ended": True}
