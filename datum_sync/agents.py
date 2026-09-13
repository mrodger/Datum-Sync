"""Agents, as a view over principals.

Since migration 017 an agent is a `service_accounts` row of `kind='agent'`
with a `parent_id`, and its token is an `account_tokens` row labelled
'agent'. The `agents` table is not read by anything; it stays for one release
as the revert path and 018 drops it.

This module keeps the function signatures the routes and tests already call
(`create`, `get`, `list_for_account`, `update_grants`, `mint_token`,
`disable`, `delete`, `public`) so the `/rest/v1/accounts/{a}/agents*` routes
keep answering while callers move to `/rest/v1/principals`. Every function
here is a thin query over the principal table; the authority rules live in
grants.py and are applied by the principal routes in api.py. The legacy
`update_grants` is the one place the rules are bent, and it says so.

What changed for a caller:

  * `create` copies the parent's authority onto the new row (capped at tier 3,
    the ceiling for `kind='agent'`), so a new agent behaves exactly as it did
    when it borrowed the parent's authority at resolve time -- and can then
    be narrowed on the Principals screen, which it never could before.
  * `resolve_token` is gone. `auth.resolve` finds agent tokens in
    `account_tokens` like any other token and `auth.effective` folds in the
    parent; there is no second lookup path to keep in step.
"""
from __future__ import annotations

import asyncpg

from datum_sync import auth, tokens

TOKEN_LABEL = "agent"

# The shape `public()` returns: what the agents routes have always returned.
_COLS = """
    sa.id, sa.parent_id AS account_id, sa.name, sa.proxy_grants, sa.disabled,
    sa.created_at, sa.last_used_at,
    EXISTS (SELECT 1 FROM account_tokens t
             WHERE t.account_id = sa.id AND t.revoked_at IS NULL
               AND (t.expires_at IS NULL OR t.expires_at > now())) AS has_token
"""


async def create(
    conn: asyncpg.Connection,
    account_id: int,
    name: str,
    proxy_grants: list[str] | None = None,
) -> tuple[asyncpg.Record, str]:
    """Create an agent under `account_id` and mint its token. Returns (row, raw).

    The raw token is returned exactly once. Only sha256(token) is stored.
    Raises `asyncpg.UniqueViolationError` if the name is taken -- principal
    names are unique across kinds.
    """
    raw = auth.new_token()
    async with conn.transaction():
        if proxy_grants:
            # As in `update_grants`: the administrator assigning a connection
            # to a new agent intends the account to hold it (PRIN-001).
            await conn.execute(
                """
                UPDATE service_accounts
                   SET proxy_grants = (
                       SELECT array_agg(DISTINCT g)
                         FROM unnest(proxy_grants || $2::text[]) AS g)
                 WHERE id = $1
                """,
                account_id,
                proxy_grants,
            )
        row = await conn.fetchrow(
            f"""
            WITH parent AS (
                SELECT max_tier, repo_scope, vault_scope, rate_limit_per_min, name
                  FROM service_accounts WHERE id = $1
            ),
            ins AS (
                INSERT INTO service_accounts
                    (name, kind, parent_id, max_tier, repo_scope, vault_scope,
                     proxy_grants, rate_limit_per_min, created_by)
                SELECT $2, 'agent', $1, LEAST(parent.max_tier, 3), parent.repo_scope,
                       parent.vault_scope, $3, parent.rate_limit_per_min, parent.name
                  FROM parent
                RETURNING *
            )
            SELECT {_COLS} FROM ins sa
            """,
            account_id,
            name,
            proxy_grants or [],
        )
        if row is None:
            raise asyncpg.ForeignKeyViolationError("no such account")
        await conn.execute(
            """
            INSERT INTO account_tokens (account_id, label, token_hash)
            VALUES ($1, $2, $3)
            """,
            row["id"],
            TOKEN_LABEL,
            auth.hash_token(raw),
        )
    return row, raw


async def get(conn: asyncpg.Connection, name: str) -> asyncpg.Record | None:
    return await conn.fetchrow(
        f"SELECT {_COLS} FROM service_accounts sa WHERE sa.name = $1 AND sa.kind = 'agent'",
        name,
    )


async def list_for_account(
    conn: asyncpg.Connection, account_id: int
) -> list[asyncpg.Record]:
    return await conn.fetch(
        f"""
        SELECT {_COLS} FROM service_accounts sa
         WHERE sa.parent_id = $1 AND sa.kind = 'agent'
         ORDER BY sa.name
        """,
        account_id,
    )


async def update_grants(
    conn: asyncpg.Connection, name: str, proxy_grants: list[str]
) -> asyncpg.Record | None:
    """Set an agent's proxy grants through the legacy route.

    The narrowing rule (grants.narrows, PRIN-001) says a child may hold only
    what its parent holds. The administrator using this route is assigning a
    connection to an agent and plainly intends the account to hold it too,
    so the parent's `proxy_grants` is widened to the union first. The
    principal routes do not do this: there the caller states both sides.
    """
    async with conn.transaction():
        row = await conn.fetchrow(
            "SELECT id, parent_id FROM service_accounts WHERE name = $1 AND kind = 'agent'",
            name,
        )
        if row is None:
            return None
        await conn.execute(
            """
            UPDATE service_accounts
               SET proxy_grants = (
                   SELECT array_agg(DISTINCT g)
                     FROM unnest(proxy_grants || $2::text[]) AS g)
             WHERE id = $1
            """,
            row["parent_id"],
            proxy_grants,
        )
        return await conn.fetchrow(
            f"""
            UPDATE service_accounts sa SET proxy_grants = $2
             WHERE sa.id = $1
            RETURNING {_COLS}
            """,
            row["id"],
            proxy_grants,
        )


async def mint_token(conn: asyncpg.Connection, name: str) -> tuple[str, bool]:
    """Replace an agent's token. Returns (raw_token, found).

    Revoke-then-mint under the fixed label, in one transaction: the previous
    token stops working at the instant the new one starts, which is what this
    route has always promised.
    """
    raw = auth.new_token()
    async with conn.transaction():
        agent_id = await conn.fetchval(
            "SELECT id FROM service_accounts WHERE name = $1 AND kind = 'agent'", name
        )
        if agent_id is None:
            return raw, False
        await tokens.revoke(conn, agent_id, TOKEN_LABEL)
        await conn.execute(
            "INSERT INTO account_tokens (account_id, label, token_hash) VALUES ($1, $2, $3)",
            agent_id,
            TOKEN_LABEL,
            auth.hash_token(raw),
        )
    return raw, True


async def disable(conn: asyncpg.Connection, name: str, value: bool) -> bool:
    result = await conn.fetchval(
        """
        UPDATE service_accounts SET disabled = $2
         WHERE name = $1 AND kind = 'agent' RETURNING id
        """,
        name,
        value,
    )
    return result is not None


async def delete(conn: asyncpg.Connection, name: str) -> bool:
    result = await conn.execute(
        "DELETE FROM service_accounts WHERE name = $1 AND kind = 'agent'", name
    )
    return result.endswith(" 1")


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
