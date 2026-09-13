# Report — WP1: principals, grants that narrow, tier as a verb ceiling

**Built** (commit follows this file):

| File | Change |
|---|---|
| `migrations/016_principals.sql` | `kind`, `parent_id` (cascade), `state`, `proxy_grants`, `limits`, `federation_scope`, `metadata`, review columns on `service_accounts`; `is_admin` derived from tier via CHECK + trigger (a write to the flag is shorthand for the tier); `kind='agent' ⇒ max_tier ≤ 3`; `account_tokens.max_tier` |
| `migrations/017_fold_agents.sql` | one principal row per agent, parent's authority copied (tier capped at 3), parent's `proxy_grants` widened to the union of its agents', agent tokens moved to `account_tokens` label `agent`; `agents` table kept for one release |
| `datum_sync/grants.py` (new) | `subsumes`, `narrows`, `intersect`, `restricted`; pure |
| `datum_sync/auth.py` | `Principal.kind/parent_id/parent_name/state/token_tier_cap/limits/federation_scope`, `effective_tier`, `authority()`; `scope_tier`, `elevation_hints`; `require_tier`; `principal_from_row` + `effective()` (state checks once, ancestor meet); agent branch removed from `resolve` |
| `datum_sync/tokens.py` | per-token `max_tier` cap, `TokenTierAbovePrincipal` |
| `datum_sync/agents.py` | rewritten as a facade over principals with the same function signatures |
| `datum_sync/principals.py` (new) | `/rest/v1/principals*` list/create/get/patch/disable/enable/delete + tokens; edit table (03 §2); PRIN-001/005/012 |
| `datum_sync/jobs.py` `execute.py` `mcp.py` `api.py` | `principal` threaded to `jobs.submit`, where the tier-3 check lives; cancel/schedules/automations tier 3; delete principal tier 5; `tools/list` tier filter; `vault_write` and proxy tier 3 |
| `datum_sync/proxy.py` `audit.py` | proxy by grant, not kind (D-22); audit actor from `kind`, sponsor in `detail.account`; mirror tables keep recording the sponsor as `account_*` |
| `datum_sync/accounts.py` | `--kind --parent --limits --federation-scope --token-tier`, `token add --max-tier`, `passwd` refuses agents |
| `datum_sync/config.py` | `POLICY_MAX_DELEGATION_DEPTH`, `POLICY_BASELINE_TIER` |
| tests | `test_grants.py` (property test found and fixed a trailing-`**` subsumption bug), `test_principals.py`, `test_tier.py`; fixtures moved off the `agents` table; tests that set `is_admin` apart from the tier now set the tier |

**Acceptance (09 WP1):**

| Item | Evidence |
|---|---|
| `narrows()` property test, 2,000 pairs | `tests/test_grants.py::test_subsumes_implies_match_implication` — passed after fixing `_walk` so a trailing `**` needs one segment, as `vault.matches` does |
| child wider → 400 naming `repo_scope` | `test_a_child_wider_than_its_parent_is_refused_naming_the_field` |
| narrowing a parent's tier → child's `whoami.effective_tier` follows, child row untouched | `test_narrowing_a_parent_narrows_its_children_at_their_next_request` |
| tier-1 token: REST submit 403 `TIER_REQUIRED`; MCP `tools/call` isError; `tools/list` hides it | `test_submitting_needs_tier_3_on_every_door`, `test_the_catalogue_hides_what_the_tier_cannot_call` |
| migrated agent authenticates with its old token and sees the same tools | `test_agent_token_resolves_to_principal`, `test_full_proxy_via_mcp` (fixtures now create agents through `agents.create`, which writes the 017 shape) |
| `test_schema_drift.py` | passes on a database rebuilt from `migrations/` |

**Gates:** `pytest -q` 723 passed, 0 skipped · `break_the_guard.py` PRIN, TIER, GRANT, AUTH, PROXY, AUDIT, MCPLOG, SESS, VAULT families all proven (192 registered) · `browser_smoke.py` passes except a pre-existing heading race on the v1 connections screen (see below) · `flow_geometry.py` PASS.

**Guards added:** GRANT-001, PRIN-001…007, PRIN-012, TIER-001…004, TIER-006, TIER-011 (WP0). `AGENT-001` moved to UNPROVABLE (held by a CHECK). `PROXY-001` re-anchored to the tier-3 check. TIER-005 is PROXY-001 and was not registered twice.

**Deviations from the spec, recorded in `11-decision-log.md`:**

- D-28: `kind='agent'` implies `max_tier ≤ 3` as a CHECK. The spec's fold copied the parent's tier unchanged, which would have made an agent under a tier-4 sponsor an administrator; the migration copies `LEAST(parent, 3)`.
- `parent_id` cascades on delete (the spec said RESTRICT). The REST route still refuses a principal with children; the cascade is for fixtures and the shell.
- `agents.create` / `update_grants` (the legacy route) widen the parent's `proxy_grants` to the union, because the administrator using that route plainly intends the account to hold what they assign. The principal routes do not.
- A write to `is_admin` on an UPDATE is shorthand for the tier in both directions (true → ≥ 4, false → ≤ 3), so the CLI's `disable`/`enable` and the old PATCH keep meaning what they said.

**Found while working:** a guard mutation (TIER-001) left a queued job in `_pytest_tier`, which made conftest's `db` fixture skip every later test and the harness report 26 AUTH guards UNPROVEN at once — exactly the failure `CLAUDE.md` warns about. The tier fixture now deletes its jobs on teardown.
