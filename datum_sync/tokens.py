"""Account tokens: many per principal, each with a label.

A service account's bearer credential used to be one column,
`service_accounts.token_hash`, so rotation meant overwriting it and breaking
every holder at that instant. These rows replace it: an account may have any
number of live tokens, and rotation is add-then-revoke with a window in
between that the operator chooses.

`label` exists so revocation can be spoken. The database cannot return a token
-- only sha256 of it is stored -- so "revoke the CI runner" is the only form
the request can take.

The read path is this table alone. `service_accounts.token_hash` is still
populated by migration 012's backfill and is deliberately never consulted
again; see the migration for why a fallback would make revocation silently
fail.

Nothing here decides whether a token is acceptable. `resolve` returns the row
with `revoked_at` and `expires_at` on it and `auth.resolve` judges them, so
that a revoked token and an expired one keep their distinct error codes
instead of collapsing into "unknown token".
"""
from __future__ import annotations

from datetime import datetime

import asyncpg

from datum_sync import auth

# The account columns a Principal is built from. Written once, here, for the
# same reason `auth.principal_by_name` gives: a new permission column added to
# one query and missed in another is a principal whose permissions quietly
# differ depending on how it authenticated.
#
# `sa.connection_grants` is deliberately absent, here and in every other
# Principal query. The column exists and is populated, but no code has ever
# read it to decide anything: whether a workspace may use a connection is
# settled by the connection's own scope/scope_targets (`connections.matches_scope`)
# and by the publisher's `max_tier`. Carrying it put a list on every Principal
# that read like a permission and enforced nothing. `agents.proxy_grants` is a
# different thing and is enforced (proxy.py).
_ACCOUNT_COLS = """
    sa.id AS account_id, sa.name, sa.max_tier, sa.repo_scope,
    sa.is_admin, sa.disabled, sa.vault_scope
"""

# Never `token_hash`. There is no read path that returns it, so no route can
# leak it by forgetting to strip it -- the same structural approach as
# `connections._COLUMNS` and the secret column.
_TOKEN_COLS = """
    t.id AS token_id, t.label, t.expires_at, t.revoked_at,
    t.created_at, t.last_used_at
"""


async def create(
    conn: asyncpg.Connection,
    account_id: int,
    label: str,
    expires_at: datetime | None = None,
) -> tuple[asyncpg.Record, str]:
    """Mint a token for an account. Returns (row, raw_token).

    The raw token is returned exactly once and cannot be recovered afterwards.
    Raises `asyncpg.UniqueViolationError` if the account already has a live
    token under this label.
    """
    raw = auth.new_token()
    row = await conn.fetchrow(
        """
        INSERT INTO account_tokens (account_id, label, token_hash, expires_at)
        VALUES ($1, $2, $3, $4)
        RETURNING id AS token_id, label, expires_at, revoked_at,
                  created_at, last_used_at
        """,
        account_id,
        label,
        auth.hash_token(raw),
        expires_at,
    )
    return row, raw


async def list_for_account(
    conn: asyncpg.Connection, account_id: int, include_revoked: bool = False
) -> list[asyncpg.Record]:
    return await conn.fetch(
        f"""
        SELECT {_TOKEN_COLS}
          FROM account_tokens t
         WHERE t.account_id = $1
           AND ($2 OR t.revoked_at IS NULL)
         ORDER BY t.created_at
        """,
        account_id,
        include_revoked,
    )


async def revoke(conn: asyncpg.Connection, account_id: int, label: str) -> bool:
    """Withdraw one token by label. True if a live token was revoked.

    Scoped to the account: a label is only unique within one, so revoking by
    label alone would let an operator aiming at their own 'ci-runner' withdraw
    somebody else's.

    Already-revoked rows are excluded rather than re-stamped, so the recorded
    revocation time stays the moment access actually ended.
    """
    result = await conn.fetchval(
        """
        UPDATE account_tokens SET revoked_at = now()
         WHERE account_id = $1 AND label = $2 AND revoked_at IS NULL
        RETURNING id
        """,
        account_id,
        label,
    )
    return result is not None


async def revoke_all(conn: asyncpg.Connection, account_id: int) -> int:
    """Withdraw every live token on an account. Returns how many."""
    rows = await conn.fetch(
        """
        UPDATE account_tokens SET revoked_at = now()
         WHERE account_id = $1 AND revoked_at IS NULL
        RETURNING id
        """,
        account_id,
    )
    return len(rows)


async def live_count(conn: asyncpg.Connection, account_id: int) -> int:
    return await conn.fetchval(
        """
        SELECT count(*) FROM account_tokens
         WHERE account_id = $1 AND revoked_at IS NULL
           AND (expires_at IS NULL OR expires_at > now())
        """,
        account_id,
    )


async def resolve(
    conn: asyncpg.Connection, token_hash: str
) -> asyncpg.Record | None:
    """Look up a presented token by its hash, joined to its account.

    Returns revoked and expired tokens too. Filtering them out here would make
    a withdrawn credential indistinguishable from one that never existed, and
    the caller would have no way to say which happened.
    """
    return await conn.fetchrow(
        f"""
        SELECT {_TOKEN_COLS}, {_ACCOUNT_COLS}
          FROM account_tokens t
          JOIN service_accounts sa ON sa.id = t.account_id
         WHERE t.token_hash = $1
        """,
        token_hash,
    )


async def mark_used(conn: asyncpg.Connection, token_id: int) -> None:
    await conn.execute(
        "UPDATE account_tokens SET last_used_at = now() WHERE id = $1", token_id
    )


def public(row: asyncpg.Record, token: str | None = None) -> dict:
    """A token as the API and CLI describe it. Raw value only on create."""
    out = {
        "label": row["label"],
        "created_at": row["created_at"],
        "expires_at": row["expires_at"],
        "last_used_at": row["last_used_at"],
        "revoked_at": row["revoked_at"],
    }
    if token is not None:
        out["token"] = token
    return out
