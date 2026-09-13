# 01 — Assessment: the codebase against the vision

Everything in this document was read out of the source at `d2d1b2c`, not out
of `README.md` or `PROPOSAL.md`. Where the two disagree, the source is quoted.

## 1. Size and shape

| Measure | Value |
|---|---|
| Python modules in `datum_sync/` | 33 files, 11,255 lines |
| Largest | `api.py` 2,329 · `oauth.py` 771 · `mcp.py` 682 · `auth.py` 667 |
| Migrations | 15 (`001_core` … `015_drop_stale_cols`), checksummed, drift-tested |
| Tests | 587 test functions across 27 modules; 178 registered guards in `tests/break_the_guard.py` |
| Web UI | two vanilla-JS single-page apps: `static/` (v1, 1,990 lines) and `static-v2/` (3,569 lines, FME-Flow look), both mounted |
| Fixtures | 6 workspaces under `repositories/` |
| Dependencies | FastAPI, asyncpg, argon2, cryptography, httpx, croniter, PyYAML — no ORM, no auth library |

The engineering quality is high and unusual: every guard has a mutation test
that must fail when the guard is deleted; every migration explains itself;
comments record what was *measured* rather than assumed. That style is worth
preserving and `10-worker-brief.md` requires it.

## 2. What exists, by component

| Component | Files | Maturity | Notes |
|---|---|---|---|
| Workspace catalogue + publish gate | `manifest.py` `repository.py` `publish.py` | production | Six-step gate, stale-from-disk, previous version stays live |
| Job engine | `jobs.py` `worker.py` `runner.py` `child.py` `execute.py` `events.py` | production | Advisory-locked single worker, `FOR UPDATE SKIP LOCKED`, pg_notify→SSE, subprocess isolation |
| Connections + secrets | `connections.py` `crypto.py` | production | AES-256-GCM, name as AAD, multi-key with key-id byte (B4) |
| Credential proxy | `proxy.py` | production | HTTP only. SSRF guard, redirect re-validation, auth injection, no bodies logged |
| Vault gate | `vault.py` `vault_fs.py` | production | Pure decision module; deny wins; `*` is one segment; coherence validated at write |
| Schedules + automations | `schedules.py` `automations.py` | production | DB-polled, YAML config, loop protection |
| Hosted services | `services.py` | production | static/pwa/dashboard; CSP; `/serve/` accepts the cookie |
| Authentication | `auth.py` `tokens.py` `agents.py` | production, with gaps (§4) | Three credentials → one `Principal`; sha256 tokens; argon2 passwords; lockout by name |
| OAuth 2.1 AS | `oauth.py` | production | PKCE S256 only, RFC 7591/8414/9728/8707/7009, refresh rotation with family revocation, exact redirect match |
| MCP server | `mcp.py` | production, narrow | Stateless `POST /mcp`; `initialize/ping/tools/list/tools/call`; workspace tools + `proxy_request` + `vault_*`; no resources, no sessions |
| Audit | `audit.py` + middleware in `api.py` | production, dual-write | Server-minted trace id; `audit_log` beside `mcp_call_log` and `proxy_log` |
| Rate limit | `ratelimit.py` | production, single-process | Per account, sliding window, in memory |
| Accounts CLI | `accounts.py` | production | The only place `repo_scope` and `vault_scope` can be *set* |
| Web UI v2 | `static-v2/app.js` `ui.py` | complete port, 6 stubs | Dashboard, Repositories, Workspaces, Run, Jobs (SSE), Schedules, Automations, Connections, Services, Admin, Account detail (tokens, agents), Analytics, Queue, System config, Notifications, "MCP Servers", Auth Services. Stubs: Streams, API endpoints, Apps, Projects, Resources, Backup |

**Does it have a GUI?** Yes, and it is further along than the README
suggests. The v2 shell is the one to build on: it has a collapsible nav rail,
sortable/paged tables, the sign-in flow, the avatar menu, an account detail
screen that mints and revokes labelled tokens and creates, disables and
deletes agents, and a `NO AUTH` badge when the dev flag is on. It renders
nothing with `innerHTML`. What it cannot do is *edit a grant*: `PATCH
/rest/v1/accounts/{name}` accepts only `disabled` and `max_tier`
(`api.py:1066`), so repository scope, vault scope and rate limit are CLI-only.
There is no enrolment, approval, session or federation screen because none of
those exist behind it.

## 3. The vision, requirement by requirement

| Requirement (from the brief) | Status | Evidence |
|---|---|---|
| Multiple users and agents authenticate to one gateway | **built** | `auth.resolve` — service-account tokens, agent tokens, OAuth access tokens, sessions all resolve to `Principal` |
| Agents are first-class, registered identities | **partial** | `agents` table is a sub-identity of an account (migration 008). An agent has no scope of its own: it inherits the account's `repo_scope`, `vault_scope`, `max_tier` in full (`auth.py:resolve`, agent branch). It cannot be narrower than its owner. |
| Stripped-down baseline configuration per agent | **missing** | Nothing distinguishes a baseline credential from a full one. `agents.py`'s docstring says "agent tokens work only for proxy"; the code gives an agent token every non-admin route. |
| "One session at a time" | **missing** | MCP is stateless by design (`mcp.py` header). No session table, no `Mcp-Session-Id`, no concurrency limit anywhere. |
| Broader access requires MCP + OAuth | **partial** | OAuth flow exists and works for Claude.ai. But an OAuth token is not *wider* than a bearer token — both resolve to the whole account — so there is nothing OAuth unlocks. `scope` is stored (`oauth_tokens.scope`) and read by nothing. |
| Human approval in the loop | **partial** | The consent screen requires a human password once. No approval queue, no device flow for headless agents, no pending-call gating. |
| MCP proxy to company resources | **missing** | `proxy.py` is an HTTP credential proxy. No MCP client exists in the codebase; no upstream MCP server can be registered. `/rest/v1/mcp-servers` lists `mcp_call_log.target` values (vault paths and `conn:METHOD:/path` strings), not MCP servers. |
| Tiered access | **partial** | Tiers gate connection *publishing* and *proxying*. They do not gate verbs: see §4.1. |
| Audit of everything | **built** | `audit_log` with server trace; middleware covers every write on every surface; 401s audited anonymously (migration 014) |
| Admin GUI | **built, read-mostly** | See §2 |
| Production deployment | **built** | systemd units, `.env`, migrations with drift test, browser smoke against the live box |

## 4. Defects and gaps found in the source

These are the things a worker must not build on top of without fixing. Each
one is scheduled in `09-build-plan.md`.

### 4.1 Tier is not a verb ceiling

`README.md` says tier 3 is required to "execute workspaces". The submit
routes check repository scope only:

```
api.py:657   auth.require_repo(caller, repo)          # REST submit
mcp.py:563   await execute.run_sync(repo, ws, submitted, MCP_SERVICE)   # MCP tools/call
```

`grep -n max_tier datum_sync/execute.py datum_sync/jobs.py datum_sync/mcp.py`
returns nothing. A tier-1 account submits, cancels and resubmits jobs, writes
the vault (if scoped), and creates schedules. The only tier checks are
`publish.py:198` (publisher vs connection tier) and `proxy.py:161` (account vs
connection tier). **The tier table in the README describes intent, not
behaviour.** Fixed by `03 §6`.

### 4.2 MCP-submitted jobs have no submitter

`execute.run_sync` takes `submitted_by: str | None = None`; `mcp.py:563` does
not pass it. Every job created through `tools/call` has `jobs.submitted_by IS
NULL`. The `audit_log` row still names the actor, but the job row — the thing
the Jobs screen and the automations engine read — does not. One-line fix,
registered as a guard so it stays fixed.

### 4.3 Agent tokens are full account principals

`agents.py` header: *"Account tokens work for everything except proxy. Agent
tokens work only for proxy."* `auth.resolve` returns a `Principal` for an
agent token with the parent's `repo_scope`, `vault_scope`, `max_tier` and
`rate_limit_per_min`, `is_admin=False`, and nothing downstream consults
`source == "agent"` or `agent_id` except `proxy.py` (requires it) and
`audit.py` (labels it). An agent can therefore submit jobs, read and write the
vault and manage schedules with the whole of its owner's authority. That is
the opposite of "stripped down". Fixed by folding agents into principals with
their own narrowing grant (`03 §2`).

### 4.4 Delegated vault scope is schema only

`jobs.delegated_vault_scope` (migration 007) is written by nothing and read by
nothing: `grep -rn delegated_vault_scope datum_sync/` is empty. The drone
delegation story in `PROPOSAL.md §3.2` does not exist. Replaced by real child
principals (`03 §2`) and a frozen grant snapshot on the job (`03 §7`).

### 4.5 OAuth scope is decorative

`oauth_codes.scope` and `oauth_tokens.scope` are carried through the flow and
appear on `Principal.scope`, which nothing reads. The consent page tells the
user the client "gains no permissions of its own", which is true and is also
why OAuth cannot be the door to broader access today. Fixed by `04 §2`.

### 4.6 No session, no concurrency limit

`mcp.py` is deliberately stateless. That was the right call for a
tool-per-request server; it is incompatible with "one session at a time".
There is also no per-principal `concurrent_jobs` or `jobs_per_hour` limit —
`rate_limit_per_min` is the only quota. Fixed by `03 §5`.

### 4.7 `/rest/v1/mcp-servers` is misnamed

It aggregates `mcp_call_log.target`, which for vault tools is a *path* and for
proxy calls is `connection:METHOD:/path`. The screen therefore shows vault
paths under a heading that says MCP servers. Replaced by the real federation
screen (`08 §5`).

### 4.8 Unbounded, unauthenticated client registration

`POST /oauth/register` is open by necessity (RFC 7591, Claude.ai has no client
id beforehand). `api.py:2283` notes 3,056 rows were observed on the demo
instance. Nothing prunes them and nothing limits the rate. `04 §5`.

### 4.9 Two audit tables still dual-written

`mcp_call_log` and `proxy_log` are written beside `audit_log` by design
(migration 011: "dual-write first, verify, then retire"). Verification has not
been recorded and the old tables still back two UI screens. `09 WP7` retires
them.

### 4.10 In-process state that breaks at two processes

`ratelimit._hits`, `auth._attempts` and (once built) the federation catalogue
cache are per-process. `ratelimit.py` says so and names the test to read when
a second process appears. The systemd unit runs one uvicorn process. This spec
keeps one process and records the constraint (`11 D-09`); it does not add a
`rate_windows` table.

### 4.11 Two authority signals

`is_admin` (boolean) and `max_tier` (1–5) are set independently. `README.md`
says tier 4 is admin; `accounts.py` lets you create a tier-4 non-admin or a
tier-2 admin. Every admin route checks `is_admin`; every connection-tier check
reads `max_tier`. Fixed by deriving `is_admin` from tier (`03 §1`).

### 4.12 Documentation drift

`README.md`: port `:8200`, "APScheduler for cron-based triggers", "Apply
migrations 001 → 006", "13 migrations". `CLAUDE.md`: port `:8201`, "Scheduler:
none — the worker polls `next_run`", 15 migrations. `CLAUDE.md` is right on
all three. The datum-gate `00-README.md` lists a `24-future-improvements.md`
that was never in the corpus. `09 WP0` fixes the README.

## 5. What the Datum-Gate corpus already settled, and what it did not

Merged (per `_BACKPORT-PLAN.md`, verified against `git log`):

| Item | Commit | Effect |
|---|---|---|
| B1 guard registry with stable ids | `dc8ce65` | `Guard: <ID>` cross-reference both ways; skips are UNPROVEN; missing tests are errors |
| B2 one `audit_log` + server trace | `c2ad34f` | `audit.Trace`, threaded explicitly |
| B5 labelled multi-token accounts | `0cbb2a9` | `account_tokens`, rotation with a window |
| B3 explicit repo scope, dead column dropped | `fbe16c3` `6adae10` | `repo_scope NOT NULL`, `['*']` is everything, `[]` is nothing |
| B4 multi-key secrets | `3950526` | key-id prefix byte |
| C1 audit the REST surface; C2 CSP on `/serve/`; C3 rate limit | `edb95c2` `3465aa6` `d2d1b2c` | |

Not merged, and why it matters here: documents 21–23 (federation, lifecycle,
tenancy) are the org-level vision this brief asks for, and the corpus review
found them to be *proposals* with concrete holes. This spec takes their
model and closes the holes rather than re-reviewing them:

| Corpus defect (`_README-review.md`) | Resolution here |
|---|---|
| Argument guard `argument_path` cannot join `owner` + `repo`; `paths` is not a grant field; list-of-object arguments unaddressable; `resources/read` unguarded | New guard grammar, `05 §4` |
| Tool-name → (connection, tool) mapping lives in process memory | Persisted `federated_tools` table, `05 §3` |
| Workspace/vault trust boundary asserted, not built (frozen scope enforced by voluntary import) | Not claimed. Jobs freeze the grant for *connections and attribution*; vault enforcement inside a subprocess is out of scope and said so (`11 D-12`) |
| Scheduled jobs intersect to an empty grant | Scheduled jobs run under the schedule owner's grant snapshot, no system principal in the meet (`03 §7`) |
| Later documents never revisited earlier ones (routes, policy, MCP list) | `07-api-surface.md` is the single list; every endpoint added here appears in it |
| Version rollback without stored bytes | Out of scope; not promised |

## 6. What is worth keeping exactly as it is

Not everything needs touching, and a worker should be told so:

- `auth.py` resolution order, token hashing, lockout, cookie rules, `DEV_PRINCIPAL` and the loopback guard. Extend, do not restructure.
- `oauth.py` in its entirety. The additions in `04` are new endpoints beside it.
- `vault.py` glob semantics and coherence rules. Reused verbatim for memory namespaces if memory is ever built.
- `proxy.py`'s SSRF guard and redirect handling. Federation reuses `validate_upstream_url` with an explicit private-origin allowance for `mcp` connections.
- `audit.py`'s explicit `Trace` threading. Federation and sessions write through it.
- The `static-v2` shell, table and nav modules, and `STYLE.md`.
- The guard harness and its rules.
