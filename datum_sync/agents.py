"""Agents: named sub-identities of a service account.

An agent is a bearer token with its own proxy grants. The service account is
the OAuth principal — it authenticates, holds repo_scope, max_tier, admin
rights. The agent is the thing that holds keys and makes proxied HTTP calls.

A new agent gets zero proxy_grants. Proxy is opt-in: the admin assigns
connections individually. The account's max_tier is the ceiling — an agent
cannot proxy a tier-3 connection if its parent account is tier-2.

Agent tokens are separate from account tokens. Account tokens work for
everything except proxy. Agent tokens work only for proxy.
"""
from __future__ import annotations

import json

import asyncpg

from datum_sync import auth

# Bare column names for single-table queries (INSERT RETURNING, UPDATE, etc.)
_COLS = """
    id, account_id, name, proxy_grants, disabled,
    created_at, last_used_at,
    (token_hash IS NOT NULL) AS has_token
"""

# Aliased for JOINs.
_COLS_A = """
    a.id, a.account_id, a.name, a.proxy_grants, a.disabled,
    a.created_at, a.last_used_at,
    (a.token_hash IS NOT NULL) AS has_token
"""

_COLUMNS_WITH_ACCOUNT = f"""
    {_COLS_A},
    sa.name AS account_name, sa.max_tier, sa.repo_scope,
    sa.is_admin, sa.disabled AS account_disabled,
    sa.vault_scope
"""


async def create(
    conn: asyncpg.Connection,
    account_id: int,
    name: str,
    proxy_grants: list[str] | None = None,
) -> tuple[asyncpg.Record, str]:
    """Create an agent and mint its token. Returns (row, raw_token).

    The raw token is returned exactly once. Only sha256(token) is stored.
    """
    raw = auth.new_token()
    row = await conn.fetchrow(
        f"""
        INSERT INTO agents (account_id, name, token_hash, proxy_grants)
        VALUES ($1, $2, $3, $4)
        RETURNING {_COLS}
        """,
        account_id,
        name,
        auth.hash_token(raw),
        proxy_grants or [],
    )
    return row, raw


async def get(conn: asyncpg.Connection, name: str) -> asyncpg.Record | None:
    return await conn.fetchrow(
        f"SELECT {_COLS} FROM agents WHERE name = $1", name
    )


async def list_for_account(
    conn: asyncpg.Connection, account_id: int
) -> list[asyncpg.Record]:
    return await conn.fetch(
        f"SELECT {_COLS} FROM agents WHERE account_id = $1 ORDER BY name",
        account_id,
    )


async def update_grants(
    conn: asyncpg.Connection, name: str, proxy_grants: list[str]
) -> asyncpg.Record | None:
    return await conn.fetchrow(
        f"""
        UPDATE agents SET proxy_grants = $2
        WHERE name = $1
        RETURNING {_COLS}
        """,
        name,
        proxy_grants,
    )


async def mint_token(conn: asyncpg.Connection, name: str) -> tuple[str, bool]:
    """Replace an agent's token. Returns (raw_token, found)."""
    raw = auth.new_token()
    result = await conn.fetchval(
        "UPDATE agents SET token_hash = $2 WHERE name = $1 RETURNING id",
        name,
        auth.hash_token(raw),
    )
    return raw, result is not None


async def disable(conn: asyncpg.Connection, name: str, value: bool) -> bool:
    result = await conn.fetchval(
        "UPDATE agents SET disabled = $2 WHERE name = $1 RETURNING id",
        name,
        value,
    )
    return result is not None


async def delete(conn: asyncpg.Connection, name: str) -> bool:
    result = await conn.execute("DELETE FROM agents WHERE name = $1", name)
    return result.endswith(" 1")


async def resolve_token(
    conn: asyncpg.Connection, token_hash: str
) -> asyncpg.Record | None:
    """Look up an agent by its hashed bearer token.

    Returns the agent row joined to its parent account, or None. The caller
    uses this to build a Principal with agent identity populated.
    """
    return await conn.fetchrow(
        f"""
        SELECT {_COLUMNS_WITH_ACCOUNT}
          FROM agents a
          JOIN service_accounts sa ON sa.id = a.account_id
         WHERE a.token_hash = $1
        """,
        token_hash,
    )


def public(row: asyncpg.Record, token: str | None = None) -> dict:
    """An agent as the API returns it. Token included only on create."""
    out = {
        "id": row["id"],
        "account_id": row["account_id"],
        "name": row["name"],
        "proxy_grants": list(row["proxy_grants"]),
        "disabled": row["disabled"],
        "has_token": row["has_token"],
        "created_at": row["created_at"],
        "last_used_at": row["last_used_at"],
    }
    if token is not None:
        out["token"] = token
    return out
