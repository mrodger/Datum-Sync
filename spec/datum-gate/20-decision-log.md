# 20 — Decision Log

Every place this design chooses differently from the original, or chooses
the same thing for a reason worth stating. Written so the implementer
never has to guess whether a difference is deliberate.

| # | Area | Original | Datum-Gate | Why |
|---|---|---|---|---|
| D1 | Identity | `service_accounts` + separate `agents` table with its own token and `proxy_grants`; OAuth grants and sessions in `oauth_tokens` | One `principals` table (`kind`, `parent_id`, `grant`) and one `credentials` table for every credential kind | The original already resolves every credential to one `Principal`; putting the rows in one place makes that structural. Agents become principals with a parent, which is the holonic "member" record directly. |
| D2 | Authority | Columns `max_tier`, `repo_scope` (NULL = all), `connection_grants`, `vault_scope` (NULL = none), `is_admin`, plus `agents.proxy_grants` | One `Grant` JSON document with explicit lists; `["*"]` for all; `[]` for none; `admin` derived from tier | Removes the documented NULL asymmetry and gives delegation something to compare with one function. |
| D3 | Delegation | `jobs.delegated_vault_scope` substituted for the account scope | Parent/child principals with `narrows()` at write time and `intersect()` at auth time; jobs freeze the effective grant | Live intersection means disabling or narrowing a parent reaches children without a tree walk; freezing on jobs keeps a run's authority fixed, which is what the original's substitution rule wanted. |
| D4 | Tiers | Five in the README, four in the schema `CHECK`, tier as a ceiling on connections | Five, with each tier's verbs stated, tier on connections and on hosted-service visibility | Makes the README's five-tier story the schema's story. |
| D5 | Tokens | One `token_hash` per account, rotate to replace | Several labelled tokens per principal, revocable individually | An agent fleet needs per-deployment tokens; a lost laptop should not rotate the drone's key. |
| D6 | Worker | One worker, advisory lock, `requeue_orphans` at start | N workers, leases with heartbeats, reaper | Same behaviour with one worker; allows more. |
| D7 | Versions | Manifest stored on `workspaces`, republish overwrites | `workspace_versions`, content-hashed, `current_version_id` pointer; jobs pin a version | Rollback is a pointer flip; unchanged syncs write nothing; a queued job runs what it was submitted against. |
| D8 | Publish gate | Six checks | Ten checks, plus venv build and `x-*` manifest extension keys | Adds the builder seam and per-workspace dependencies without changing the contract's shape. |
| D9 | Parameter types | `STRING INTEGER FLOAT BOOLEAN FILE LOOKUP_CHOICE` | `STRING INTEGER FLOAT BOOLEAN FILE CHOICE GEOMETRY JSON` with constraints | `CHOICE` is the same thing renamed; `GEOMETRY` uses the PostGIS that is already required; `JSON` with a schema is what a builder emits. |
| D10 | MCP exposure | Derived from `data_streaming` | Explicit `mcp` in `services` | A workspace author says which surfaces it is on; deriving one from another was a coupling that had to be documented. |
| D11 | Long-running MCP tools | Synchronous wait up to timeout + margin | `_wait_seconds` then a job handle; `job_status` / `job_result` tools; MCP resources for artifacts | A 20-minute workspace can be a tool. |
| D12 | Automations | `job_complete` only; two actions; at-most-once firing; failures recorded per action | Four triggers, four actions, durable `deliveries` outbox with dedupe keys and backoff; chain-depth loop detection | At-least-once with dedupe is what "deliver the email" needs; the A→B→C→A gap is closed. |
| D13 | Automation authority | Actions run with the worker's authority | `owner_grant` frozen at save, intersected with the owner's current grant at fire | An automation is a standing instruction from a person; it should not outlive or outgrow that person's authority. |
| D14 | Audit | `mcp_call_log`, `proxy_log`, `job_log`, `automation_runs` | One `audit_log` for every surface plus the high-volume tables, all joined by a server-minted `trace_id` | One question, one table. |
| D15 | Trace id | Client `X-Trace-Id` stored, untrusted | Server mints one per request and propagates it into jobs, deliveries and audit; client value kept beside it | Correlation that does not depend on the client. |
| D16 | Secrets | Single key, no rotation | Key id byte in the blob, multiple keys, `reseal` | Rotation without downtime. |
| D17 | Vault promote | `promote` as a glob list, single-actor | Promote as `src -> dst` edges; `promotions` table; two-phase by policy | An edge says where content may go; a second principal is the control the proposal asked about. |
| D18 | Governance paths | Hardcoded set in `mcp.py` | `policy.yaml`, with a write-tier floor and reload | Observable and changeable without a deploy. |
| D19 | Vault graph tools | Cross-database FDW into the orchestrator DB | `search` with a pluggable backend; an external index is an `http` connection whose results are re-filtered by scope | The gateway stays independent of any other database; the scope filter is enforced regardless of what the index knows. |
| D20 | Hosted services | Static + unbuilt supervised family | Static + reverse `proxy`; no supervised family | Supervision is a different machine; a proxy fronts whatever runs it. Matches the proposal's Phase 4. |
| D21 | Service visibility | Session cookie accepted; no per-service policy | `public` / `principal` / `tier` per service | The proposal's "Drone Monitor requires auth" becomes a column. |
| D22 | Rate limits | Consent-screen lockout only; `rate_limit_per_min` column unused | `limits` on the grant, enforced per principal, per connection for proxy, by table when multi-process | Makes the column real. |
| D23 | REST paths | `/rest/v1/transformations/...` (FME-shaped) | `/rest/v1/jobs/...` with 301 aliases for the old four | Familiar to existing clients; not named after a runtime that is gone. |
| D24 | JSON from asyncpg | Per-module decode helpers | Codecs registered on the pool | Removes a class of "string where dict expected" defects. |
| D25 | Child harness | `datum_sync.child` inside the package | `datumgate_child` as its own top-level package | `PYTHONPATH` can point at the harness alone; the gateway package is not importable by workspace code. |
| D26 | UI | Two static trees (v1, v2 in progress), FME Flow geometry parity | One tree, same tokens, no external geometry target | The visual identity is kept; pixel parity with another product is not a build goal here. |
| D27 | Account creation over HTTP | None (CLI only) | First principal CLI-only; further principals via REST under the narrowing rule | Delegation needs an HTTP path; the bootstrap does not. |
| D28 | Password on agents | Nullable column on every account | Passwords only for `kind='human'` | A credential nobody should use should not be possible. |
| D29 | Idempotency on schedules | None | `schedule:{id}:{next_run}` key on the submit | A crash between submit and advance re-fires safely. |
| D31 | Upstream MCP servers | Not modelled; agents configure servers directly | `mcp` connection type; gateway federates tools/resources under grant + argument guards; agents get one URL | The gateway is the thing that makes an agent capable; unfederated servers are unaudited access. |
| D32 | Shared memory | Vault files + an external graph index over another DB | Built-in Postgres memory provider with namespaces, kinds, history, full-text (+ optional pgvector) | The gateway already owns a Postgres; memory scoped by namespace globs reuses the vault's glob and deny rules. |
| D33 | Agent lifecycle | `disabled` flag; "revoke drops to tier 1" described | Explicit states (pending/active/restricted/disabled/retired), registration codes, claim, review cadence, inactivity rules | Org-level management needs the states named and the transitions audited. |
| D34 | Tenancy | Repositories only | Teams as a fence on principals, connections, repositories, services; org-wide by NULL | One gateway, one audit, teams as labels — cross-team is a tier-5 grant. |
| D35 | Activity review | Raw log tables | Rollups, per-agent summary, deterministic anomaly signals, review queue, monthly report | "Review their activity" is a product surface, not a query. |
| D36 | Argument-level control | Tool-level (tool listed or not) | Argument guards mapping upstream args to grant globs; approval-gated calls | Federated tools are opaque; the repo/host/folder in the arguments is the thing that needs a scope. |
| D30 | Guards | `break_the_guard.py` with cases in Python | `guards.yaml` registry with stable ids referenced from the spec, CI cross-check | The registry is the security spec's table of contents. |

## Things kept exactly

- Postgres as the queue; `pg_notify` as a hint; polling as the guarantee.
- `FOR UPDATE SKIP LOCKED` claims.
- Manifest stored verbatim at publish; disk edits do not change a
  published interface.
- Spec to the child on stdin; stdout redirected before import; process
  group kill; `PR_SET_PDEATHSIG`.
- Config/secret split; AES-GCM with the name as AAD; `_COLUMNS` never
  containing `secret`.
- Deny wins; allows literal; 403 never 404 for out-of-scope vault paths.
- Cron in the schedule's zone; intervals as durations; missed fires once;
  re-enable re-times.
- YAML `safe_load`; verbatim storage; self-trigger refused inside `parse`.
- Egress checks on every redirect hop.
- Hand-rolled OAuth 2.1 with PKCE S256 only, audience binding, refresh
  rotation with family revocation, exact redirect matching, name-keyed
  lockout checked before the database.
- The artifact is the site; re-run swings the URL; old artifacts stay.
- No `innerHTML`; no CDN; cookie accepted on `/serve/` and refused on the
  executing service paths.
- The five-section `MANIFEST.md` and the meaning of "empty".
- The holonic vocabulary.
