# 03 — Principals, grants, lifecycle, sessions

The load-bearing document. Every other component asks it one question:
*given this request, what is the effective grant, and does it permit this
verb on this target?*

## 1. One principal table

`service_accounts` stays the table (renaming it touches every query and
buys nothing; `11 D-01`). It gains the columns that make an agent a
principal rather than a sub-identity:

| Column | Type | Meaning |
|---|---|---|
| `kind` | `TEXT NOT NULL CHECK (kind IN ('human','agent','system'))` default `'human'` | Descriptive. `human` may hold a password; `agent` may not. Never an authority input. |
| `parent_id` | `INTEGER NULL REFERENCES service_accounts(id)` | Delegation tree. NULL is a root. Depth ≤ `POLICY_MAX_DELEGATION_DEPTH` (4). |
| `state` | `TEXT NOT NULL CHECK (state IN ('pending','active','restricted','disabled','retired','rejected'))` default `'active'` | Lifecycle, §4. Replaces `disabled` in a second migration; until then `disabled` is a generated read of `state <> 'active' AND state <> 'restricted'`. |
| `proxy_grants` | `TEXT[] NOT NULL DEFAULT '{}'` | Moved from `agents`. Connections this principal may forward through. |
| `limits` | `JSONB NOT NULL DEFAULT '{}'` | `{concurrent_sessions, jobs_per_hour, concurrent_jobs}`; a missing key means the policy default. `rate_limit_per_min` stays a column because it is enforced and tested as one. |
| `federation_scope` | `JSONB NULL` | The `code`/`compute`/`documents`/`mcp` blocks from `05 §2`. NULL means no federated access — same direction as `vault_scope`. |
| `metadata` | `JSONB NOT NULL DEFAULT '{}'` | model, host, owner contact, purpose. Free-form, shown in the UI, never an authority input. |
| `restricted_from` | `JSONB NULL` | Snapshot of the authority columns taken at restrict, for exact restore. |
| `review_due_at`, `last_reviewed_at`, `last_reviewed_by` | timestamps / text | §4.3 |
| `created_by` | `TEXT` | principal name |

`is_admin` becomes **derived**: `is_admin = (max_tier >= 4)`. Migration 016
rewrites the column from the tier, and `accounts.py` / `PATCH
/rest/v1/accounts` refuse to set it independently. The column stays so that
thirty call sites of `caller.is_admin` keep reading; a check constraint holds
the two together (`06 §016`).

### 1.1 Folding `agents` into principals

Today: `agents(id, account_id, name, token_hash, proxy_grants, disabled)`.
After migration 017, each row becomes a `service_accounts` row with
`kind='agent'`, `parent_id=account_id`, the same `name`, `proxy_grants`
copied, `max_tier`, `repo_scope`, `vault_scope` **copied from the parent**
(so nothing narrows on the day of the migration — the existing behaviour is
preserved, then narrowed by an operator), and its `token_hash` becomes an
`account_tokens` row with `label='agent'`. `auth.resolve` loses its agent
branch; `Principal.agent_id/agent_name` are replaced by `kind` and
`parent_id`, and `audit.write` records `actor_kind` from `kind`.

The `agents` table is dropped one migration later, after the API routes under
`/rest/v1/accounts/{a}/agents` are re-pointed at `/rest/v1/principals` (they
stay as aliases for one release — `07 §1`).

`proxy.check_proxy_access` changes its first check from "must be an agent"
to "must hold the connection in `proxy_grants`" — a human with a proxy grant
is no less entitled than an agent with one, and the old rule existed only
because accounts had no `proxy_grants` column.

## 2. Grants that narrow

A principal's authority is the tuple

```
(max_tier, repo_scope, vault_scope, proxy_grants, federation_scope, limits, rate_limit_per_min)
```

**Invariant PRIN-001:** for every principal with a parent, `narrows(child,
parent)` is true at every write to either row.

`narrows(child, parent)` in a new `datum_sync/grants.py` (pure, no DB, like
`vault.py`):

| Field | Rule |
|---|---|
| `max_tier` | `child ≤ parent` |
| `repo_scope` | every child pattern is matched by some parent pattern (`*` matches all; `X/*` ≡ `X`); `['*']` ⊆ only `['*']` |
| `vault_scope` | for `read`, `write`, `quarantine`, `promote`: every child pattern is **subsumed** by some parent pattern (`grants.subsumes`, the segment-walk from datum-gate `03 §6`, with `**` absorbing zero or more segments); `deny`: every parent pattern subsumed by some child pattern (child denies at least as much); NULL child ⊆ anything; NULL parent ⊇ only NULL child |
| `proxy_grants` | set inclusion |
| `federation_scope` | per block, `05 §2.2` |
| `limits` | each child value ≤ parent value, missing = policy default compared as such |
| `rate_limit_per_min` | child ≤ parent; NULL parent means unlimited |

**Effective grant** (`grants.effective(rows)`): walk `parent_id` to the root
(one recursive CTE, `06 §016`), then `effective = own`, and for each
ancestor nearest-first `effective = intersect(effective, ancestor)`. If any
ancestor's `state` is not `active`, the request fails `401
ANCESTOR_<STATE>`. `intersect` is the pairwise meet: min tier, pattern
intersection via subsumption (conservative: a pattern survives only if the
other side subsumes it), union on deny, min on limits. Because PRIN-001 held
at write time, `intersect` is normally the identity; it exists so that
narrowing or disabling a sponsor takes effect on its agents on the next
request, without a tree walk at write time.

`Principal` gains `kind`, `parent_id`, `state`, `credential_kind`
(`token|oauth|session|local|auth-disabled`), `token_tier_cap` and `scope`.
`Principal.effective_tier` is `min(max_tier, token_tier_cap or 5,
scope_tier(scope) or 5)`; **it is what `require_tier` reads, never
`max_tier`**.

**Who may edit a grant** (unchanged from datum-gate `03 §5.3`, adopted):

| Actor tier | May set on target |
|---|---|
| 5 | anything |
| 4 | any principal whose effective grant ⊆ actor's, to any grant ⊆ actor's, tier ≤ 4 |
| 3 | own children only, to any grant narrowing the actor's own |
| ≤ 2 | nothing |

An edit that would make any descendant stop narrowing is refused with `409
DESCENDANTS_WOULD_WIDEN` naming them. Silent re-narrowing via `intersect`
would hide the change from the person making it.

## 3. Credentials

Unchanged: `account_tokens` (bearer, sha256), `oauth_tokens` (access /
refresh / session), `password_hash`. Two additions:

- `account_tokens.max_tier INTEGER NULL` — a per-token ceiling. Minted by
  `POST /rest/v1/principals/{name}/tokens {label, max_tier?, expires_at?}`;
  refused if above the principal's tier. The enrolment claim mints the
  baseline token with `max_tier = POLICY_BASELINE_TIER` (2). `whoami`
  reports `token_tier_cap`.
- `oauth_tokens.scope` becomes enforced (`04 §2`).

`tokens.resolve` returns `t.max_tier` alongside the account columns and
`auth.resolve` folds it into `Principal.token_tier_cap`. Nothing else in the
resolver changes, and its three-branch order (OAuth, then account tokens,
then nothing) is kept; the agent branch is deleted by §1.1.

## 4. Lifecycle

```
             ┌─────────┐ approve  ┌────────┐ restrict ┌────────────┐
   enrol ───►│ pending ├─────────►│ active ├─────────►│ restricted │
             └────┬────┘          └───┬────┘◄─────────┴────────────┘
                  │ reject / ttl      │ disable           restore
                  ▼                   ▼
             ┌──────────┐        ┌──────────┐ retire  ┌─────────┐
             │ rejected │        │ disabled ├────────►│ retired │
             └──────────┘        └──────────┘         └─────────┘
```

| State | Authenticates? | Effective grant |
|---|---|---|
| `active` | yes | full |
| `restricted` | yes | forced to tier 1: `max_tier=1`, `vault_scope` reduced to its `read` block, `proxy_grants=[]`, `federation_scope=NULL`, `limits.concurrent_sessions=1`. The agent can still `whoami` and read why (`restricted_reason` on the row). |
| `pending`, `disabled`, `retired`, `rejected` | 401 with the state upper-cased as the error code (`PRINCIPAL_PENDING` …) | — |

Transitions are REST verbs on `/rest/v1/principals/{name}/…` (`07 §1`), each
audited with `principal.<verb>` and `governance=true`. `restrict` stores the
authority tuple in `restricted_from`; `restore` puts back exactly that and
nothing else (guard `PRIN-010`). `retire` requires children retired first or
`?cascade=true`; it revokes every credential, keeps every row. `DELETE` stays
tier-5 and is for mistakes.

### 4.1 Enrolment

`registration_codes(id, code_hash, created_by, parent_id, template JSONB,
max_uses, uses, expires_at, auto_approve, created_at, revoked_at)`.

`POST /enrol` is **public, rate-limited by code hash** (5 failures per 15
min per code, same shape as the password lockout — reuse `auth._locked_for`
with a namespaced key). Body: `{code, name, metadata, requested?}` where
`requested` is a partial authority tuple that MUST narrow `template`.
Creates a `pending` (or `active` if `auto_approve`) principal of
`kind='agent'` under the code's `parent_id`, and returns a single-use
`claim_code` (24 h) or, when auto-approved, the baseline token.

`POST /rest/v1/principals/{name}/approve {grant edits?}` (tier ≥4, or the
sponsor for scope ⊆ own) → `active`, `review_due_at = now() +
POLICY_REVIEW_INTERVAL_DAYS`. `POST /enrol/claim {claim_code}` then mints the
baseline token exactly once.

### 4.2 Names

Agent names are unique across all principals (the column already is). The
enrolment endpoint refuses names that collide and names matching
`^(system|admin|superuser)`. Metadata `model` and `host` are recorded and
shown; they are not verified.

### 4.3 Review housekeeping (worker tick, daily)

- `pending` older than `POLICY_PENDING_TTL_DAYS` (7) → `rejected`, reason `expired`.
- `active` with `last_used_at` older than `POLICY_INACTIVITY_RESTRICT_DAYS` (30) → `restricted`, audited `principal.auto_restrict`.
- `review_due_at` passed → flagged on the Principals screen and on `/health` as `reviews_overdue`. No automatic restriction on review lag in phase 1.

Humans (`kind='human'`) are exempt from inactivity restriction.

## 5. Sessions and the concurrency limit

The MCP transport permits a server to assign a session id on `initialize`
and to require it thereafter. This is used for exactly two things:
**counting** and **audit grouping**. No tool state lives in a session; every
request is still fully authenticated by its bearer token, as today.

`mcp_sessions(id UUID PK, principal_id, credential_kind, client_name,
client_version, protocol_version, started_at, last_seen_at, ended_at,
end_reason CHECK IN ('client','idle','revoked','limit','restart'))`.

Rules:

- `initialize` counts live sessions for the principal (`ended_at IS NULL AND last_seen_at > now() - SESSION_IDLE_SECONDS`). At or over `limits.concurrent_sessions` (policy default 1 for baseline, 4 for elevated) → JSON-RPC error `-32000 SESSION_LIMIT` with `data: {active: [{session_id, started_at, idle_seconds, client_name}]}`. Otherwise INSERT and return `Mcp-Session-Id`. Guard `SESS-001`.
- Every other `/mcp` request MUST carry `Mcp-Session-Id`; a missing header → `400 SESSION_REQUIRED` (HTTP, not JSON-RPC, per the transport); an unknown or ended id → `404` so the client re-initialises. A session id presented with a different principal's token → `404`, never a hint (guard `SESS-002`).
- Each request bumps `last_seen_at`. `DELETE /mcp` with the header ends it (`client`). Idle sessions end lazily: they are counted as ended when `last_seen_at` is stale, and the daily tick stamps `ended_at`/`idle` for the record.
- Revoking the credential or restricting the principal ends its sessions (`revoked`) in the same transaction — a live session is not a way to outlive a revocation (guard `SESS-003`).
- `audit_log.session_id UUID NULL` is written on every `/mcp` row so a session's activity reads as a group.
- A principal with `concurrent_sessions = 0` may not `initialize` at all; this is what `restricted` does *not* set, deliberately, so a restricted agent can still ask why.

Backward compatibility: clients that never send the header (scripts using
`/mcp` as a plain RPC) get one grace: `initialize` is optional when the
principal's `limits.concurrent_sessions` is absent **and** the credential is
a human's token. Agents always need a session. This keeps `browser_smoke.py`
and the agent harness working until they are updated in the same work
package.

### 5.1 Job limits

`limits.jobs_per_hour` and `limits.concurrent_jobs` are enforced in
`jobs.submit` by counting rows (`submitted_by = name AND submitted_at > now()
- interval '1 hour'`, and `status IN ('queued','running')`). Always the
database, never memory. `429 JOB_LIMIT` with the count. Guard `TIER-010`.

## 6. Tier as a verb ceiling

One function, `auth.require_tier(principal, n, verb)`, raises `403
TIER_REQUIRED {"required": n, "effective": principal.effective_tier,
"elevate": "<scope that would satisfy it>"}`. The `elevate` hint is what an
agent's operator needs to read to know which OAuth scope to ask for.

| Verb | Tier | Call site |
|---|---|---|
| authenticate, `whoami`, list repositories and workspaces in scope, vault `read`/`list` | 1 | — |
| read jobs, logs, artifacts; `job_status`, `job_result`; vault `quarantine` writes | 2 | `api.get_job` etc., `mcp._vault_call` |
| submit / cancel / resubmit jobs (REST, MCP, `/stream`, `/download`, `/upload`); vault `write`; `proxy_request`; create/edit schedules and automations; federated tools with `requires.write` | 3 | `jobs.submit` (**the one place**, so every door pays), `mcp._proxy_call`, schedule/automation routes |
| manage connections; create/edit/approve/restrict principals with tier ≤ 4 and grant ⊆ own; approve pending calls; publish | 4 | existing `require_admin` sites, which become `require_tier(4)` |
| create tier-5 principals; key rotation; delete principals; create `mcp` connections with private origins | 5 | — |

`jobs.submit` gains a `principal` parameter and performs the tier-3 check
and the §5.1 limits itself; `execute.run_sync` passes the principal through
and sets `submitted_by=principal.name` (fixes `01 §4.2`). The scheduler and
automation engine submit as the schedule/automation owner's snapshot (§7).
Guard `TIER-001`: deleting the check in `jobs.submit` fails a test that
submits over REST **and** one over MCP.

The MCP catalogue hides tools whose minimum tier exceeds
`principal.effective_tier` ("fewer tools rather than broken ones", as
`mcp._tools_list` already does for vault tools). Workspace tools carry
`manifest.mcp.min_tier` (default 3, since calling one submits a job).

## 7. Frozen grant on jobs

`jobs.grant_snapshot JSONB` — the authority tuple at submit, plus
`principal_name`, `kind`, `session_id`, `trace_id`. Written by
`jobs.submit`; read by the worker to (a) resolve connections against the
snapshot's `max_tier` rather than the live row, (b) attribute artifacts, (c)
serve `job_status`/`job_result` only to the submitter or a tier-4 principal
in scope. `delegated_vault_scope` is dropped in the same migration; a
narrower run is a child principal submitting, not a per-job override.

Scheduled and automation-triggered jobs snapshot the **owner's** current
effective grant at fire time. There is no `system:worker` principal in the
meet, which is the corpus defect that produced empty grants.

## 8. `whoami`

`GET /rest/v1/whoami` and the MCP `whoami` tool return:

```json
{"name": "datum-main", "kind": "agent", "state": "active", "parent": "marcus",
 "max_tier": 3, "effective_tier": 2, "token_tier_cap": 2, "scope": null,
 "credential_kind": "token", "session": {"id": "…", "started_at": "…"},
 "limits": {"concurrent_sessions": 1, "jobs_per_hour": 20, "concurrent_jobs": 1},
 "repo_scope": ["SCIMAC"], "vault_scope": {...}, "proxy_grants": [],
 "federation": {"code": {"connections": ["github"], "repos": ["datum/*"], "write": false}},
 "review_due_at": "…", "restricted_reason": null,
 "elevate": {"mcp:operate": "unlocks tier 3: submit jobs, vault write, proxy, federated writes"}}
```

The `elevate` block is computed from the tier table so an agent can explain
to its operator what it cannot do and what to ask for.

## 9. Guards

| id | Guard | Test must fail when |
|---|---|---|
| PRIN-001 | child grant must narrow parent at write | `narrows()` call removed from principal create/update |
| PRIN-002 | effective grant intersects ancestors | `intersect` loop removed from resolve |
| PRIN-003 | non-active ancestor → 401 | ancestor state check removed |
| PRIN-004 | `is_admin` derived from tier | PATCH accepting `is_admin` |
| PRIN-005 | grant edit refused if a descendant would widen | descendant re-validation removed |
| PRIN-006 | tier-3 may edit own children only | subtree check removed |
| PRIN-007 | agent principals never hold a password | `kind` check removed from `passwd` |
| PRIN-008 | `restricted` forced to tier 1 | restriction override removed from resolve |
| PRIN-009 | `pending/disabled/retired/rejected` → 401 with state code | state check removed |
| PRIN-010 | `restore` returns exactly `restricted_from` | snapshot read replaced by live row |
| PRIN-011 | retire revokes every credential | token revocation removed from retire |
| PRIN-012 | delegation depth bounded | depth check removed |
| ENRL-001 | enrol code single-use accounting and expiry | `uses < max_uses` check removed |
| ENRL-002 | requested grant must narrow template | `narrows()` removed from enrol |
| ENRL-003 | claim code single-use, 24 h | `claimed_at` check removed |
| ENRL-004 | enrol rate-limited per code | lockout call removed |
| ENRL-005 | auto-approve codes need tier 4 to mint | tier check removed |
| SESS-001 | second session refused at the limit | count check removed |
| SESS-002 | session id bound to its principal | principal comparison removed |
| SESS-003 | revocation ends live sessions | session update removed from revoke |
| SESS-004 | agents cannot skip `initialize` | grace branch widened to agents |
| TIER-001 | submit requires tier 3 on REST and MCP | `require_tier` removed from `jobs.submit` |
| TIER-002 | token cap lowers effective tier | `min()` with `token_tier_cap` removed |
| TIER-003 | scope cap lowers effective tier | `min()` with `scope_tier` removed |
| TIER-004 | catalogue hides tools above effective tier | tier filter removed from `_tools_list` |
| TIER-005 | proxy requires tier 3 | check removed from `_proxy_call` |
| TIER-006 | vault write requires tier 3 | check removed from `_vault_call` |
| TIER-010 | `concurrent_jobs` / `jobs_per_hour` enforced in the DB | count removed from `jobs.submit` |
| TIER-011 | MCP-submitted job records `submitted_by` | argument dropped from `run_sync` call |
