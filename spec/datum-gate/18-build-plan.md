# 18 — Build Plan

Twelve steps. Each is usable before the next starts, ships its migrations,
its tests, and its guard registry entries, and has an acceptance gate. Run
the four gates (`pytest -q`, `python tests/break_the_guard.py`,
`ruff check && mypy --strict datumgate`, and from step 8 `browser_smoke.py`)
before calling a step done.

## Step 1 — Foundation

**Build:** `config.py` (env + policy.yaml, startup refusals), `db.py`
(pool, JSON codecs, `transaction()` helper), `errors.py` (envelope,
handlers), `models.py` (Pydantic: `Grant`, `Manifest`, `Policy`),
`audit.py` (queue writer), `cli/migrate.py`, migrations `000`, `001`,
`002`, `009`.

**Accept:**
- `python -m datumgate migrate` applies and records; re-running is a no-op;
  a modified applied file is refused.
- `Grant` validation rejects every case in `03 §3` with a message naming
  the field.
- `subsumes()` and `intersect()` pass a property test: for random patterns
  and random paths, `subsumes(a,b) ⇒ ∀p: match(b,p) ⇒ match(a,p)`.
- Audit writer batches; overflow counts.
- Guards: GRANT-011…016, AUDIT-003.

## Step 2 — Principals and authentication

**Build:** `authority/*`, `cli/principals.py`, `surfaces/rest.py` skeleton
with middleware (trace, auth, audit, rate limit), `/health`, `/whoami`,
`/rest/v1/principals*`, `/ui/signin|signout` (API only, no shell yet).

**Accept:**
- CLI creates a tier-5 human; a token authenticates; `whoami` shows the
  effective grant.
- Creating a child via REST with a non-narrowing grant → 400
  `GRANT_NOT_NARROWER` naming the field.
- Disabling a parent → child's token → 401 `ANCESTOR_DISABLED`.
- Narrowing a parent's repositories → child's `whoami` shows the narrower
  effective set without any write to the child.
- Lockout behaves per `03 §9`.
- Guards: AUTH-001…014, GRANT-001…010.

## Step 3 — Catalogue and publish

**Build:** `catalogue/*`, `cli/sync.py`, migration `003`, REST catalogue
endpoints, `/schema` endpoint, venv builder.

**Accept:**
- `sync` publishes the `Testing/*` fixtures; a second `sync` reports
  `unchanged` for all.
- Editing a fixture's `main.py` and syncing produces a new version; the
  old is `superseded`; `activate` on the old version flips
  `current_version_id` back.
- Every publish-gate failure in `05 §5` is reproduced by a fixture and
  leaves the previous version current.
- Guards: PUB-001…016.

## Step 4 — Job engine

**Build:** `engine/*`, `datumgate_child/`, migration `004`, job REST
endpoints, uploads, SSE.

**Accept:**
- `Testing/echo_file`, `slow`, `chatty`, `site` fixtures run end to end.
- SIGKILL the worker mid-`slow`: child dies (assert via process group),
  job requeued within one lease, completes on a restarted worker.
- Two workers with concurrency 1 and 10 queued jobs: each job runs once.
- SSE replay + live: a client connecting after 30 log lines and
  reconnecting with `Last-Event-ID` sees no gaps and no duplicates.
- `/stream` returns the primary body; `?wait=1` on `slow` → 504 with job id.
- Guards: JOB-001…026.

## Step 5 — Connections and proxy

**Build:** `connections/*`, migration `005`, REST connection endpoints,
`keys` CLI, publish-gate connection checks (`05 §5` #6) now live.

**Accept:**
- Create a `database` connection with a secret; list and get never show
  it; test succeeds against the compose Postgres.
- A job's `connections` dict carries the opened secret; the child env does
  not.
- Key rotation: add key 2, `reseal`, remove key 1, every connection still
  opens.
- Proxy through an `http` connection to a local mock (registered with
  `allow_private_origin` by tier 5) injects auth and strips caller auth;
  SSRF tests with a resolver stub.
- Guards: CONN-001…017.

## Step 6 — Vault gate

**Build:** `vault/*`, migration `008`, vault REST endpoints, policy
governance paths, promotions.

**Accept:**
- Scope tests from `08 §3` as a table-driven suite (≥60 cases).
- Two-phase promotion: tier-2 agent quarantine-writes, tier-3 requests
  promotion, tier-4 approves, file moves, sidecars written, audit shows
  four rows under two traces.
- Governance write by tier 3 with a matching grant → 403 with
  `governance: true` in the audit row.
- Guards: VAULT-001…022.

## Step 7 — MCP and OAuth

**Build:** `surfaces/mcp.py`, `surfaces/oauth.py`, `.well-known`
documents.

**Accept:**
- An MCP conformance script (`tests/mcp_client.py`) does
  initialize → tools/list → tools/call on `Testing/echo_file`
  → resources/read of the artifact, with a bearer token.
- `slow` with `_wait_seconds=1` returns a job handle; `job_result` after
  completion returns the output.
- A full PKCE flow with a scripted client: register, authorize (form
  post), token, use on `/mcp`, refresh, replay the old refresh → family
  revoked → the new access token is dead too.
- `delegate_create` from an agent token creates a narrowed child and the
  child's `tools/list` is smaller.
- Guards: OAUTH-001…010, MCP-001…011.

## Step 8 — Web UI

**Build:** `static/*`, `surfaces/ui.py`, `browser_smoke.py`.

**Accept:** every screen in `15 §4` reaches `data-ready`; the smoke script
completes; UI-001…004 guards.

## Step 9 — Schedules and automations

**Build:** `triggers/*`, migration `006`, REST endpoints, `/hooks/{path}`,
worker ticks.

**Accept:**
- A cron schedule in `Pacific/Auckland` computes `next_run` correctly
  across a DST boundary (fixed dates in the test).
- An automation with four actions fires on `Testing/site` completion;
  killing the worker between two deliveries and restarting re-runs only
  the unfinished ones; the `run_workspace` delivery's retry returns the
  same job id.
- Cycle A→B→C→A is refused at fire time with an audit row.
- Webhook trigger with HMAC and nonce replay.
- Guards: TRIG-001…025.

## Step 10 — Hosted services

**Build:** `surfaces/serve.py`, migration `007`, service REST endpoints,
proxy kind with WebSocket pump, CSP injection.

**Accept:**
- `Testing/site` produces `/serve/_fixture-site/`; re-running swings the
  path; "revert" swings it back.
- A proxy service to a local echo server forwards headers per `13 §5`,
  never `Authorization`; a `tier` service refuses tier 1 with 403.
- Guards: SERVE-001…009.

## Step 11 — Audit surface and retention

**Build:** `/rest/v1/audit*`, trace view, `retention sweep`, `audit export`.

**Accept:**
- The trace view for one MCP call shows the audit rows, the job, its log
  head, and the deliveries it caused.
- Sweep deletes per policy and never a served directory; governance rows
  survive.
- Guards: AUDIT-001…006.

## Step 12 — Memory provider

**Build:** `memory/*`, migration `010`, `memory_*` MCP tools, `/rest/v1/memory`, UI Memory screen.

**Accept:** two agents in different namespaces cannot see each other's private entries; `org/**` write by tier 3 → 403 governance; every put has a history row; `expect_version` conflict → 409; search ranks title matches above body; pgvector absent → full-text only, no error.
Guards: MEM-001…006.

## Step 13 — Federation and core resources

**Build:** `federation/*`, `mcp` connection type, profiles, argument guards, pending calls, migration `010b`, UI Federation + Approvals screens.

**Accept:** with a mock upstream MCP server (`tests/mock_mcp_server.py` exposing github-, ssh- and drive-shaped tools): catalogue merges under prefixes; `default: deny` hides unguarded tools; `gh__push_files` on a repo outside `code.repos` is denied before any upstream call (mock asserts zero requests); `vm__run_command` with `sudo` denied by the deny regex; `vm__restart_service` creates a pending call, approval forwards it under the requester's grant; upstream down → tool omitted, audit row, `tools/list` still answers within 5 s.
Guards: FED-001…015.

## Step 14 — Lifecycle and tenancy

**Build:** `lifecycle/*`, migrations `011`, `012`, `/register`, `/register/claim`, approve/restrict/restore/retire/review endpoints, teams, rollups, signals, review queue, UI Review/Teams screens, activity summary.

**Accept:** self-registration with a code creates `pending`; approval issues one token via claim; `restricted` agent gets tier-1 tools only; a team connection is unusable from another team even when named in the grant; the daily tick restricts an idle agent and flags a `denied_burst`; the activity summary for S4's agent matches the audit spine.
Guards: LIFE-001…010, TEAM-001…004, REV-001…004.

## Step 15 — Hardening and reference scenarios

**Build:** nothing new; run `19-reference-scenarios.md` end to end (S1–S10),
close every gap, confirm all four gates on a clean checkout, write
`CLAUDE.md` for the repository from `00-README.md` and `16-config-deploy.md`.

**Accept:** all scenarios pass; `guards.yaml` has no UNPROVEN entries;
`/health` reports `ok` under a two-worker, two-API-process deployment for
one hour of the `chatty` fixture on a 30-second interval schedule.

## Fixtures

Ship these under `repositories/Testing/` from step 3, each with a full
`MANIFEST.md`:

| Workspace | Purpose |
|---|---|
| `echo_file` | optional FILE param, echoes it back — exercises uploads and MCP FILE omission |
| `chatty` | emits 200 log lines and progress — SSE, truncation |
| `slow` | sleeps `SECONDS` — leases, cancel, timeout, job handles |
| `site` | returns a `service/static` directory — hosted services |
| `failing` | raises — isError, failure paths |
| `needs_db` | declares a `database` connection and runs `SELECT 1` — connection resolution |
| `vault_touch` | declares a `file` connection into the vault and writes a file — frozen-grant vault enforcement |
| `geom` | `GEOMETRY` param validated by PostGIS |
