# Report — WP7: retire the mirror tables

**Built:**

| File | Change |
|---|---|
| `tools/verify_audit_mirror.py` (new) | pairs every `mcp_call_log` row with an `audit_log` row of the same actor (or sponsor in `detail.account`), verb, minute and target, and every `proxy_log` row with a `proxy.request` row of the same agent, connection, method, minute and upstream status; prints the disagreement table as markdown; exit 1 on any unmatched row |
| `spec/agent-auth-plane/_verify-audit-mirror.md` | the run against this box before 023: 908 of 908 `mcp_call_log` rows matched and agreed; `proxy_log` empty in the window; the 102 `denied` audit rows and the verbs the mirror never had are the whole difference |
| `migrations/023_retire_mirrors.sql` | `DROP TABLE mcp_call_log; DROP TABLE proxy_log;` |
| `datum_sync/mcp.py` | `_log_call` writes the one audit row |
| `datum_sync/proxy.py` | `_audit_log` writes the one audit row |
| `datum_sync/api.py` | Analytics `mcp` block (`total`, `top_tools`) from `audit_log` (`via = 'mcp'`, `detail.tool`); `/rest/v1/mcp-servers` targets from `audit_log`, bounded to 100 (the v2 MCP Servers screen reads the federation catalogue and does not use it) |
| `datum_sync/audit.py`, `auth.py` | docstrings no longer describe a dual write |
| tests | `test_mcp_call_log.py` → `test_mcp_audit.py` (same unit tests; e2e rows read from `audit_log` in the mirror's shape); `test_audit_trace.py` agreement test → `test_the_audit_row_carries_the_call_shape`; `test_proxy.py`, `test_proxy_e2e.py`, `test_audit_middleware.py`, `test_dashboard_routes.py`, `test_agent_harness.py`, `test_tier.py` re-pointed; PROXY-006 retired from the registry, MCPLOG-003 re-anchored on `audit.write`, DASH-004 re-anchored |

**Acceptance (09 WP7):**

| Item | Evidence |
|---|---|
| verification report committed with its numbers | `_verify-audit-mirror.md` |
| both screens render from `audit_log` | Analytics and the (v1) MCP targets route read `audit_log`; the v2 MCP Servers screen reads `/rest/v1/federation` since WP5; `test_dashboard_routes.py::test_other_callers_mcp_targets_are_not_readable` seeds `audit_log` |
| AUDIT-001…009 still proven | harness run recorded below |

**Deviations:**

- The `mcp_call_log.account_name` semantics (an agent's *sponsor*) are gone with the table; `audit_log.actor_name` is the agent and `detail.account` its sponsor, which is what the verification paired on.
- `mcp-servers` counts `outcome <> 'ok'` as errors, which now includes `denied` — the count the old route could not give.
- The numbers in the verification report are this development box's (test and smoke traffic). Run the tool on the deployed instance before applying 023 there; it exits non-zero on any unmatched row, and the migration is not applied by `git pull` alone.
