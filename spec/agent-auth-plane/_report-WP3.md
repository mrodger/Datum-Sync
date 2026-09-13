# Report — WP3: sessions, job limits, the frozen grant, job built-ins

**Built:**

| File | Change |
|---|---|
| `migrations/019_sessions.sql` | `mcp_sessions` (with `superseded`), `audit_log.session_id`, `jobs.grant_snapshot` |
| `datum_sync/sessions.py` (new) | `open` (limit, same-credential supersession under an advisory lock), `touch`, `close`, `end_for_principal`, `end_for_credential`, `sweep_idle`; `/rest/v1/principals/{name}/sessions` list and end |
| `datum_sync/mcp.py` | `initialize` admits a session and answers `Mcp-Session-Id`; other methods present it (404 on an unknown or another principal's; agents get `SESSION_REQUIRED` without one, humans one release of grace); `DELETE /mcp`; `-32000 SESSION_LIMIT` with the live sessions named; built-ins `whoami`, `session_info`, `job_status`, `job_result`, `job_list`, `job_cancel` shown by tier; `_wait_seconds` and the job handle on timeout; tool names capped at 48 with a stable hash suffix |
| `datum_sync/jobs.py` | `jobs_per_hour` / `concurrent_jobs` counted in the database (`JobLimit` → 429); `grant_snapshot` written at submit; `claim` returns it |
| `datum_sync/connections.py` `worker.py` | connections resolve against the snapshot's effective tier when the job has one |
| `datum_sync/auth.py` `audit.py` `tokens.py` `oauth.py` `lifecycle.py` | `Principal.credential_id`; `Trace.session_id` and `audit_log.session_id`; revoking a token, a family, restricting or retiring a principal ends its live sessions |
| `datum_sync/execute.py` | `wait_seconds`, session and trace threaded to submit |
| `datum_sync/static-v2/app.js` | Sessions panel on the Principal screen (live count, limit, idle window, end) |
| tests | `test_sessions.py`, `test_harness_conformance.py` (cases 1, 2, 5); agent-token MCP tests now initialise a session |

**Acceptance (09 WP3):**

| Item | Evidence |
|---|---|
| second `initialize` on a baseline token names the first session; after `DELETE /mcp` it succeeds | `test_a_second_credential_is_refused_at_the_limit` (a second token), `test_delete_ends_the_session_and_session_info_describes_it` |
| another principal's session id → 404, no hint | `test_a_session_is_bound_to_its_principal` |
| revoking the token mid-session → 401 next request, row shows `revoked` | `test_revoking_the_token_ends_its_session` |
| same token re-`initialize` supersedes (Claude Code); five open/call/close cycles never hit the limit (Datum-3.0) | conformance cases 1 and 2 |
| the worker resolves connections against `grant_snapshot` | `connections.resolve(max_tier=…)` from the snapshot in `worker._execute`; the snapshot carries `effective_tier` (`test_job_limits_are_counted_in_the_database` asserts its shape) |
| `jobs_per_hour=2`: third submit → 429 `JOB_LIMIT` | `test_job_limits_are_counted_in_the_database` |

**Guards added:** SESS-001…006, MCP-020, TIER-010.

**Deviations:** the conformance script drives the wire with httpx in the request shapes the Python `mcp` SDK sends rather than through the SDK, which is not a dependency here. `job_status` returns the last five log lines rather than progress percentages: progress is only in `pg_notify` payloads, not on the row. The self-service Sessions screen under the avatar menu is the Principal screen's panel for one's own row; a dedicated screen is not added.
