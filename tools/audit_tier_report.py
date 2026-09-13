"""List every principal at tier 3 or above with its live credentials.

    .venv/bin/python tools/audit_tier_report.py

Run before migration 017 (spec/agent-auth-plane/06-schema.md). The fold of
`agents` into `service_accounts` copies each agent's *parent* tier onto the
agent row, because that is the authority the agent token already carried
(`auth.resolve` built its Principal from the parent). Nothing narrows on the
day of the migration, and this report is how an operator sees what that
preserves: every agent listed here at tier >= 3 is one to narrow afterwards.

Reads only. Prints one line per principal and one indented line per live
credential (account tokens by label, agent tokens by agent name, OAuth refresh
grants by client). Raw tokens are not stored and cannot be shown.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import asyncpg  # noqa: E402

from datum_sync import config  # noqa: E402


async def main() -> int:
    conn = await asyncpg.connect(config.DATABASE_URL)
    try:
        accounts = await conn.fetch(
            """
            SELECT id, name, max_tier, is_admin, disabled, repo_scope,
                   (vault_scope IS NOT NULL) AS has_vault
              FROM service_accounts
             WHERE max_tier >= 3
             ORDER BY max_tier DESC, name
            """
        )
        if not accounts:
            print("no principals at tier 3 or above")
            return 0
        for a in accounts:
            flags = " ".join(
                f for f, on in (("admin", a["is_admin"]), ("disabled", a["disabled"]),
                                ("vault", a["has_vault"])) if on
            )
            print(f"{a['name']:24} tier {a['max_tier']}  scope={list(a['repo_scope'])}  {flags}")
            for t in await conn.fetch(
                """
                SELECT label, expires_at, last_used_at FROM account_tokens
                 WHERE account_id = $1 AND revoked_at IS NULL
                   AND (expires_at IS NULL OR expires_at > now())
                 ORDER BY label
                """,
                a["id"],
            ):
                print(f"    token   {t['label']:20} last used {t['last_used_at'] or 'never'}")
            agents = await conn.fetch(
                """
                SELECT name, disabled, proxy_grants, last_used_at,
                       (token_hash IS NOT NULL) AS has_token
                  FROM agents WHERE account_id = $1 ORDER BY name
                """,
                a["id"],
            ) if await conn.fetchval("SELECT to_regclass('agents') IS NOT NULL") else []
            for ag in agents:
                state = "disabled" if ag["disabled"] else ("token" if ag["has_token"] else "no token")
                print(
                    f"    agent   {ag['name']:20} {state:9} inherits tier {a['max_tier']}  "
                    f"proxy={list(ag['proxy_grants'])}  last used {ag['last_used_at'] or 'never'}"
                )
            for g in await conn.fetch(
                """
                SELECT client_id, count(*) AS n FROM oauth_tokens
                 WHERE account_id = $1 AND kind = 'refresh' AND revoked_at IS NULL
                   AND rotated_to IS NULL AND (expires_at IS NULL OR expires_at > now())
                 GROUP BY client_id
                """,
                a["id"],
            ):
                print(f"    oauth   client {g['client_id']}  {g['n']} live grant(s)")
        return 0
    finally:
        await conn.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
