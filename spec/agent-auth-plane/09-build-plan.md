# 09 — Build plan: work packages for a worker model

Nine packages. Each is one worker session (or two for WP4 and WP5), ships
its migration, its tests and its guard registry entries, and ends with all
four gates green: `pytest -q` · `python tests/break_the_guard.py <PREFIX>`
· `python tests/browser_smoke.py` · `python tests/flow_geometry.py`. A
package is not done while a guard it introduced reads UNPROVEN.

Ordering is by dependency, not by value. WP1 is the only package that
touches the resolver's shape; everything after it reads `effective_tier`
and `kind`. WP4 and WP5 are independent of each other and of WP6.

```
WP0 ─► WP1 ─► WP2 ─► WP3 ─┬─► WP4 (OAuth elevation)
                          ├─► WP5 (federation)  ─► WP6 (approvals + review)
                          └─► WP7 (retire mirrors)
                                            WP8 (UI consolidation, teams seam)
```

## WP0 — Truth, and the two one-line fixes

**Build:** README corrected to the code (`01 §4.12`); `mcp.py` passes
`submitted_by=principal.name` to `run_sync`; `agents.py` header comment
corrected to what the code does (it is replaced in WP1, but a wrong
docstring must not survive a release); a `tools/audit_tier_report.py` that
lists every principal and agent with tier ≥ 3 and its live credentials, so
the operator knows what WP1's fold will preserve.

**Accept:** TIER-011 registered and proven. README's port, scheduler and
migration statements match `CLAUDE.md`.

## WP1 — Principals: one table, grants that narrow, tier as a ceiling

**Build:** migrations 016, 017; `datum_sync/grants.py` (`narrows`,
`subsumes`, `intersect`, `effective`, `matches`); `auth.resolve` drops the
agent branch, reads `kind/parent_id/state`, computes the effective tuple
and `effective_tier`; `auth.require_tier`; `jobs.submit(principal=…)` with
the tier-3 check and limits; `execute.run_sync` threads the principal;
every `require_admin` site becomes `require_tier(4)`; `PATCH` refuses
`is_admin`; `accounts.py` CLI gains `--kind`, `--parent`, `--limits`,
`--federation-scope`, `--token-tier`; `/rest/v1/principals*` routes with the
old `/accounts*` as aliases; `whoami` per `03 §8` (without session fields).
`proxy.check_proxy_access` reads `proxy_grants` from the principal.

**Accept:**
- `narrows()` property test: for random patterns and paths, `subsumes(a,b) ⇒ ∀p: match(b,p) ⇒ match(a,p)`, 2,000 cases.
- Creating a child with a wider `repo_scope` → 400 naming `repo_scope`.
- Narrowing a parent's tier → child's `whoami.effective_tier` follows with no write to the child.
- A tier-1 token: REST submit → 403 `TIER_REQUIRED`; MCP `tools/call` on a workspace → `isError` with the same code; `tools/list` does not list it.
- A migrated agent authenticates with its old token, sees the same tools as before the migration (snapshot test using the fixture DB), and appears in `/rest/v1/principals` under its parent.
- `test_schema_drift.py` passes.
- Guards: PRIN-001…007, PRIN-012, TIER-001…006, TIER-010.

## WP2 — Lifecycle and enrolment

**Build:** migration 019; `datum_sync/lifecycle.py` (transitions, snapshot
/ restore, retire cascade); `datum_sync/enrol.py` (codes, `/enrol`,
`/enrol/claim`, per-code lockout reusing `auth._locked_for`); the worker's
daily tick (pending expiry, inactivity restriction, review-overdue flag);
`/health.reviews_overdue`; Principals list/detail screens and the Enrolment
screen (`08 §2–3`); `browser_smoke.py` enrols and approves an agent.

**Accept:**
- Enrol with a code → `pending`; approve → `active`; claim → token with `max_tier=2`; second claim → 400.
- `restrict` then `restore` returns the authority tuple byte-equal to before.
- `retire` on a principal with active children → 409; with `?cascade` → all retired, every token `revoked_at` set, sessions ended.
- A pending row older than 7 days is rejected by the tick with reason `expired`.
- Guards: PRIN-008…011, ENRL-001…005.

## WP3 — Sessions and job limits

**Build:** migration 020; `datum_sync/sessions.py`; `Mcp-Session-Id` on
`initialize`, required thereafter for agents, `DELETE /mcp`;
`SESSION_LIMIT` with same-credential supersession; tool-name cap at 48; `audit_log.session_id`; sessions ended on revoke/restrict;
`jobs.grant_snapshot` written and read by the worker for connection
resolution and attribution; `job_status` / `job_result` / `job_list` /
`session_info` built-ins; Sessions panel and self-service screen;
`test_agent_harness.py` updated to initialise sessions.

**Accept:**
- Two `initialize` calls with a baseline token: the second → `-32000` naming the first session's id and age; after `DELETE /mcp` on the first, the second succeeds.
- A request with another principal's session id → 404, no hint.
- A second `initialize` on the **same** token without `DELETE` supersedes the first (Claude Code reconnect); on a different token it is refused. Five `initialize`/`tools/call`/`DELETE` cycles on one token (Datum-3.0 shape) never hit the limit. `tests/harness_conformance.py` cases 1, 2 and 5.
- Revoking the token mid-session → the next request 401 and the session row shows `revoked`.
- The worker resolves a job's connections against `grant_snapshot.max_tier`; narrowing the principal after submit does not change a running job.
- `jobs_per_hour=2`: third submit in an hour → 429 `JOB_LIMIT`.
- Guards: SESS-001…006, MCP-020, TIER-010 (extended to `concurrent_jobs`).

## WP4 — OAuth elevation

**Build:** migration 021 (device codes; `denied` outcome); scope vocabulary
and `scope_tier` in `auth`; `datum_sync/device.py` for `/oauth/device` and
the device grant on `/oauth/token`; consent page scope description, scope
picker and `on_behalf_of`; Connect panel snippets; registration rate limit and pruning; Approvals › Elevations
screen; `elevate` built-in; `browser_smoke.py` elevates an agent by device
code.

**Accept:**
- A scripted client: device request with a baseline token → poll →
  `authorization_pending` → approve in UI → token → `whoami.effective_tier == 3`; poll again → `invalid_grant` and the family revoked.
- Scope `mcp` on a tier-5 principal cannot submit a job.
- `on_behalf_of` by a non-sponsor → 403 on the consent page, no code minted.
- 31st registration in an hour → 429.
- A Codex-shaped registration (no `scope`, versioned `client_name`) followed by an authorize with `scope=mcp:operate` succeeds; a Claude-Code-shaped authorize with no scope shows the picker. `tests/harness_conformance.py` cases 3, 4 and 6.
- Guards: ELEV-001…012.

## WP5 — Federation (two sessions)

**Session A — client and catalogue.** `connections` type `mcp`; migration
022; `datum_sync/federation/client.py` (streamable-HTTP MCP client on httpx:
`initialize`, `tools/list`, `resources/list`, `tools/call`,
`resources/read`; auth injection; SSRF with private-origin allowance;
timeouts; size caps); `catalogue.py` (refresh tick, `federated_tools`
upsert, clash handling); `tools/list` merge with tier and block filters;
`tests/mock_mcp_server.py` exposing github-, ssh- and drive-shaped tools;
Connections form for `mcp`; `/rest/v1/federation*` read routes.

**Session B — guards and calls.** `guards.py` implementing the grammar
(`05 §4`): JSONPath subset, `join`, `const`, `optional`, `commands`,
`requires`, `resolve: drive_folder` with cache; validation against cached
schema; `tools/call` dispatch; `resources/read` guards; profiles as data;
Federation screen with guard coverage and the "as principal" picker.

**Accept:**
- With the mock upstream: `gh__push_files` on `datum/gateway` with `code.repos ["datum/*"]` and `write:true` is forwarded; on `other/repo` denied **before** any upstream request (mock counter is 0); with `write:false` denied; `gh__delete_repository` not listed.
- `vm__run_command` with `sudo ls` denied by the deny regex; `ls -la` allowed; missing `host` → denied.
- `drive__get_file` on a file whose resolved folder is outside `folders` → denied; resolver failure → denied.
- Upstream stopped: `tools/list` answers within 5 s with the cached catalogue; a call returns `isError UPSTREAM_UNAVAILABLE`; `federation_status.last_error` set.
- A federated tool named like a workspace tool is dropped with an audit row.
- `audit_log` shows `federate.call` with `detail = {"repos": "datum/gateway"}` and nothing else from the arguments.
- Guards: FED-001…008, FED-011…020.

## WP6 — Approval-gated calls and the review queue

**Build:** `pending_calls` handling (request, expire, approve/reject/execute
under snapshot); `pending_status` / `pending_result` built-ins; Approvals ›
Calls tab; `/rest/v1/review` and the Review screen; activity rollup route
(computed live from `audit_log` in phase 1; a rollup table only if the
90-day query exceeds 500 ms on the demo data); dashboard tile.

**Accept:**
- `vm__restart_service` → pending; approve → forwarded once under the requester's snapshot even after the requester was narrowed; result retrievable by the requester only; reject → `access_denied` on fetch; expiry after 72 h by the tick.
- `/rest/v1/review` lists a pending enrolment, a pending elevation, a pending call, an overdue review and a stale upstream, each with a link that resolves.
- Guards: FED-009, FED-010, REV-001 (activity visible to self, sponsor, tier 4 only), REV-002 (approve requires sponsor-or-4), REV-003 (expired pending calls cannot be approved).

## WP7 — Retire the mirror tables

**Build:** `tools/verify_audit_mirror.py` comparing `mcp_call_log` and
`proxy_log` against `audit_log` for the retention window and printing the
disagreement table; Analytics and Auth Services screens re-pointed at
`audit_log`; migration 023; `mcp._log_call` and `proxy._audit_log` write
one row each.

**Accept:** the verification report is committed under `spec/agent-auth-plane/_verify-audit-mirror.md` with its numbers; both screens render from `audit_log`; AUDIT-001…009 still proven.

## WP8 — Consolidation

**Build:** remove the v1 shell (`/ui` redirects to `/ui/v2`, `static/`
deleted, `ui.py` mounts one directory); `tests/browser_smoke.py` runs
against v2 only; `datumMinTier` annotations and `elevate` hints checked in
the smoke; `teams` seam recorded but **not built** (`11 D-14`);
`CLAUDE.md` updated with the new modules, gates and the "one process"
constraint; `spec/agent-auth-plane/00-README.md` status flipped to
"implemented".

**Accept:** all four gates on a clean checkout; `break_the_guard.py` with no
argument reports every id proven or UNPROVABLE with a stated reason; a
fresh database built from `migrations/` matches the demo database
(`test_schema_drift.py`).

## Sizing

| WP | Files touched (est.) | New tests (est.) | Guards |
|---|---|---|---|
| 0 | 4 | 2 | 1 |
| 1 | 12 + 2 migrations | 60 | 14 |
| 2 | 8 + 1 migration + 2 screens | 40 | 9 |
| 3 | 9 + 1 migration + 1 screen | 35 | 5 |
| 4 | 7 + 1 migration + 1 screen | 35 | 10 |
| 5 | 10 + 1 migration + 2 screens + mock server | 70 | 18 |
| 6 | 6 + 2 screens | 25 | 5 |
| 7 | 5 + 1 migration | 8 | 0 |
| 8 | many deletions | 5 | 0 |

## What a worker must not do in any package

- Skip, disable or quarantine a test to get a gate green.
- Rewrite a checksummed migration.
- Add a second process, thread pool or background task that holds authority state.
- Log an argument value, a vault body, a token or a secret. `audit.write` stays the only writer and `detail` stays identifiers.
- Add a dependency for OAuth, MCP or JSON-RPC. httpx is the client; the server is hand-rolled like `oauth.py`.
- Change `spec/datum-gate/*` or `spec/overview.md`; if this spec is wrong, edit this spec and say so in `11-decision-log.md`.
