"""Compare the two mirror tables against audit_log before retiring them.

spec/agent-auth-plane/06 §023 and 09 WP7. For the last N days (default 90)
this pairs every `mcp_call_log` row with an `audit_log` row of the same
actor, verb and minute, and every `proxy_log` row with a `proxy.request`
audit row of the same agent, connection and minute, then prints the
disagreement table as markdown. The count differences it expects:

* audit rows with outcome `denied` (federation guards, WP5) -- the mirror
  never wrote that outcome, which is the recorded reason it retires;
* audit rows for verbs the mirror never had (sessions, lifecycle, OAuth,
  federation, enrolment, REST middleware rows);
* an `mcp_call_log.account_name` that is the agent's *sponsor* while the
  audit row's actor is the agent itself (the mirror predates agents being
  rows), which the pairing allows for.

    python tools/verify_audit_mirror.py [--days N] > spec/agent-auth-plane/_verify-audit-mirror.md
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime, timezone

import asyncpg

sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parent.parent))
from datum_sync import config  # noqa: E402


async def main(days: int) -> int:
    conn = await asyncpg.connect(config.DATABASE_URL)
    try:
        have = {r["table_name"] for r in await conn.fetch(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'")}
        if not {"mcp_call_log", "proxy_log"} <= have:
            print("mirror tables are already gone; nothing to verify")
            return 0
        window = f"created_at > now() - make_interval(days => {int(days)})"

        mcp_total = await conn.fetchval(f"SELECT count(*) FROM mcp_call_log WHERE {window}")
        mcp_matched = await conn.fetchval(f"""
            SELECT count(*) FROM mcp_call_log m
             WHERE {window} AND EXISTS (
               SELECT 1 FROM audit_log a
                WHERE a.via = 'mcp' AND a.verb = 'mcp.' || replace(m.method, '/', '.')
                  AND date_trunc('minute', a.created_at) = date_trunc('minute', m.created_at)
                  AND (a.actor_name = m.account_name OR a.detail->>'account' = m.account_name)
                  AND a.target IS NOT DISTINCT FROM m.target)
        """)
        mcp_outcome_agree = await conn.fetchval(f"""
            SELECT count(*) FROM mcp_call_log m
             WHERE {window} AND EXISTS (
               SELECT 1 FROM audit_log a
                WHERE a.via = 'mcp' AND a.verb = 'mcp.' || replace(m.method, '/', '.')
                  AND date_trunc('minute', a.created_at) = date_trunc('minute', m.created_at)
                  AND (a.actor_name = m.account_name OR a.detail->>'account' = m.account_name)
                  AND a.target IS NOT DISTINCT FROM m.target
                  AND a.outcome = m.outcome AND a.governance = m.is_governance
                  AND a.error_code IS NOT DISTINCT FROM m.error_code)
        """)
        audit_mcp = await conn.fetchval(
            f"SELECT count(*) FROM audit_log WHERE via = 'mcp' AND verb LIKE 'mcp.%' AND {window}")
        audit_denied = await conn.fetchval(
            f"SELECT count(*) FROM audit_log WHERE outcome = 'denied' AND {window}")
        audit_other = await conn.fetch(f"""
            SELECT split_part(verb, '.', 1) AS family, count(*) AS n FROM audit_log
             WHERE {window} AND NOT (via = 'mcp' AND verb LIKE 'mcp.%') AND verb <> 'proxy.request'
             GROUP BY family ORDER BY n DESC
        """)
        proxy_total = await conn.fetchval(f"SELECT count(*) FROM proxy_log WHERE {window}")
        proxy_matched = await conn.fetchval(f"""
            SELECT count(*) FROM proxy_log p
             WHERE {window} AND EXISTS (
               SELECT 1 FROM audit_log a
                WHERE a.verb = 'proxy.request'
                  AND date_trunc('minute', a.created_at) = date_trunc('minute', p.created_at)
                  AND (a.actor_name = p.agent_name OR a.actor_name = p.account_name)
                  AND a.target LIKE p.connection_name || ':' || p.method || ':%'
                  AND (a.detail->>'upstream_status')::int = p.upstream_status)
        """)
        audit_proxy = await conn.fetchval(
            f"SELECT count(*) FROM audit_log WHERE verb = 'proxy.request' AND {window}")
        unmatched_mcp = await conn.fetch(f"""
            SELECT m.account_name, m.method, m.tool_name, m.target, m.outcome, m.created_at
              FROM mcp_call_log m
             WHERE {window} AND NOT EXISTS (
               SELECT 1 FROM audit_log a
                WHERE a.via = 'mcp' AND a.verb = 'mcp.' || replace(m.method, '/', '.')
                  AND date_trunc('minute', a.created_at) = date_trunc('minute', m.created_at)
                  AND (a.actor_name = m.account_name OR a.detail->>'account' = m.account_name)
                  AND a.target IS NOT DISTINCT FROM m.target)
             ORDER BY m.created_at DESC LIMIT 10
        """)
    finally:
        await conn.close()

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    print(f"# Audit mirror verification — {now}, last {days} days\n")
    print("Generated by `tools/verify_audit_mirror.py` against this box's database "
          "before migration 023 dropped the mirrors. A mirror row *matches* when an "
          "`audit_log` row has the same actor (or, for an agent, the sponsor in `detail.account`), "
          "verb, minute and target; it *agrees* when outcome, governance and error code match too.\n")
    print("| Table | Rows | Matched in audit_log | Agree on outcome/governance/error | audit_log rows for the same verbs |")
    print("|---|---|---|---|---|")
    print(f"| `mcp_call_log` | {mcp_total} | {mcp_matched} | {mcp_outcome_agree} | {audit_mcp} |")
    print(f"| `proxy_log` | {proxy_total} | {proxy_matched} | — | {audit_proxy} |\n")
    print("**Differences explained:**\n")
    print(f"- `audit_log` rows with outcome `denied` in the window: {audit_denied}. The mirror "
          "recorded a refused federated call as `ok` (the tool result carried `isError`), which "
          "is the disagreement D-18 named as the reason to retire it.")
    print(f"- `audit_log` rows for verbs the mirror never had: "
          + ", ".join(f"`{r['family']}` {r['n']}" for r in audit_other) + ".")
    print(f"- `mcp_call_log` rows the pairing could not match: {mcp_total - mcp_matched}"
          + (" (listed below)." if unmatched_mcp else "."))
    if unmatched_mcp:
        print("\n| account | method | tool | target | outcome | at |\n|---|---|---|---|---|---|")
        for r in unmatched_mcp:
            print(f"| {r['account_name']} | {r['method']} | {r['tool_name'] or ''} | {r['target'] or ''} "
                  f"| {r['outcome']} | {r['created_at'].isoformat(timespec='seconds')} |")
    print("\n**Verdict:** " + (
        "every mirror row has an audit row; the mirrors can be dropped."
        if mcp_matched == mcp_total and proxy_matched == proxy_total
        else "unmatched rows above need a look before migration 023 is applied."))
    return 0 if (mcp_matched == mcp_total and proxy_matched == proxy_total) else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=90)
    sys.exit(asyncio.run(main(ap.parse_args().days)))
