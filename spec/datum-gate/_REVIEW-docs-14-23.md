# Review — Datum-Gate documents 14–23

Reviewed 2026-09-05. Documents 14, 15, 16, 17, 19, 21, 22, 23 read in full.
`datumgate` has no implementation; this reviews text, not running code.
No `guards.yaml` exists anywhere.

---

## 14 — Audit spine

One append-only `audit_log`, joined by a **server-minted** `trace_id`; high-volume
tables (`job_log`, `automation_runs`, `deliveries`) stay separate but carry the same id.

**Concrete:** 14 columns; `client_trace_id` explicitly "untrusted, never used for auth
or dedupe". `via` ∈ `rest mcp ui cli worker serve oauth`; `outcome` ∈ `ok|denied|error`.
`detail` capped at **2 KiB** with `detail.truncated`. Writer is a bounded in-process
queue, `AUDIT_QUEUE_MAX` **10 000**, `COPY` batches of **≤200**; queue full → drop and
increment `audit_dropped`, exposed on `/health` — *"an audit gap is reported, never
silent."* `write_sync` for denied-governance, grant changes, key ops. ~60 verbs.
An MCP `tools/call` writes **two** rows under one trace. Retention `audit_days` 365,
`governance_audit_days` 0 = forever; the deletion is itself audited. Bulk export is
CLI-only — *"the CLI is the trust boundary for bulk extraction."*

**Gaps:**
- **Verb vocabulary is stale against 21/22/23.** Missing: all `federate.*`, all
  `memory.*`, all `principal.approve/reject/restrict/restore/retire/review`,
  pending-call verbs, `register`/`register.claim`. Conversely it lists
  `principal.enable`/`disable`, which 22's state machine replaces.
- **The `governance` flag drives retention and its policy list omits the new surfaces.**
  `policy.governance_verbs` in `16 §2` has no memory-`org/**` write, no pending-call
  approval, no restrict/restore — all declared governance-flagged by 21/22. Those rows
  get swept at 365 days.
- **No team dimension.** 23 scopes activity by team; `14 §4`'s tier-4 rule never
  mentions `team_id`. Two answers to "what may a tier-4 read".
- `target` is specified only as "a safe identifier… never content" — **no grammar**,
  yet `23 §3.1`'s `by_resource` rollup must parse it.
- AUDIT-001 is worded "every non-GET REST request writes a row", but `10 §1` also
  audits content-yielding GETs. The guard proves half its rule.

---

## 15 — Web UI

Vanilla-JS ES modules, no build step, no CDN, served at `/ui/`, talks only to `/rest/v1/`.

**Concrete:** `innerHTML`/`outerHTML`/`insertAdjacentHTML`/`document.write`/`eval`/
`Function` banned and grepped by `test_ui_source.py`; every node via `h()`. Shell CSP
`default-src 'self'; frame-ancestors 'none'`. 13 `screens/*.js` + 7 support modules.
Health dot polls `/health` every 30 s; red **NO AUTH** badge when
`credential_kind == 'dev'`. Dashboard quick-run tiles for the 8 most-run workspaces.
Cron helper previews next 5 fires. Automations as a `<textarea>` with **verbatim YAML
round-trip** (good — avoids a lossy structured editor). SSE reconnect via
`Last-Event-ID`, re-reading status from the first frame "rather than trusting local
state". Destructive actions need the name typed. Tokens navy `#1D3A5C`, amber
`#C89632`, dark `#0F1923`; tier chips 1–5 grey/blue/green/amber/red.

**Gaps:**
- **Screen inventory incomplete as of 23.** 23 §4 adds Review, Teams, Memory,
  Approvals, Federation — five screens absent from 15's sidebar, file list and smoke
  script. Build step 8 accepts on "every screen in `15 §4`", so steps 12–14 ship UI
  with no acceptance criterion in the document that owns UI acceptance.
- **`POST /rest/v1/schedules/preview` and `POST /rest/v1/principals/validate` exist
  nowhere else** — zero occurrences in `10-rest-api.md`. The UI doc invents API.
- **"No external origins" vs the visual tokens.** DM Sans / Space Grotesk /
  JetBrains Mono are webfonts; no font files in `static/`, and UI-002 greps for
  `http://`. The branded typography never renders.
- Sidebar hides by **tier, not scope** — `19`'s `robert` is tier 3 with an empty vault
  scope, so he gets a Vault nav item that 403s on every click.
- `md.js` is required by the Workspace screen and tested by UI-003, but missing from
  the §2 file list.
- No screen for `registration_codes` anywhere in 15 or 23.

---

## 16 — Config, policy, deploy, ops

24-row env table, four named startup refusals, full `policy.yaml`, two systemd units,
CLI surface, backup boundaries, `/health` shape, upgrade rule.

**Concrete:** startup **refuses** (naming the variable) when `PUBLIC_URL` absent;
`DG_AUTH=off` with non-loopback `HOST`; `DG_ACTIVE_KEY_ID` names an absent key; any
`DG_SECRET_KEY_n` isn't 32 bytes. Port 8200; `ACCESS_TOKEN_TTL` 3600;
`REFRESH_TOKEN_TTL` 30 d; lockout 5/900 s; `JOB_LEASE_SECONDS` 60;
`MAX_UPLOAD_BYTES` 256 MB; `VAULT_MAX_READ/WRITE` 256 KiB / 4 MiB;
`PROXY_MAX_RESPONSE_BYTES` 1 MiB; `DEFAULT_TIMEZONE` `Pacific/Auckland`.
`policy.yaml`: `governance_paths` = `SOUL.md`, `skills/**`, `hooks/**`, `policy/**`;
`governance_write_min_tier: 4`; `promote.require_second_principal: true`,
`approver_tier: 4`, `pending_ttl_hours: 72`; `delegation.max_depth: 4`;
`automation.max_chain_depth: 8`. systemd: `NoNewPrivileges`, `ProtectSystem=strict`,
`PrivateTmp`. *"There is no HTTP route that creates the first principal."*
`/health` → `degraded` when no worker heartbeat in 60 s **or** `audit_dropped > 0`.
Migrations forward-only; a drop ships in **two** releases.

**Gaps:**
- **`policy.yaml` is missing three whole blocks that 21/22/23 reference** —
  `policy.memory.*`, `policy.lifecycle.*`, `policy.review.*`. §2 says an invalid
  policy file **refuses to start** and is schema-validated. So a validating loader
  plus a 21/22/23-era policy file **is a startup failure**.
- **Key-name contradiction:** `03 §5` says `policy.max_delegation_depth`; 16 defines
  `delegation.max_depth`. GRANT-016 tests whichever the implementer guesses.
- **pgvector is never installed** — §4 creates `postgis` and `pgcrypto` only, while
  `21 §3.1` declares `embedding VECTOR(1536)`. Default deployment silently gets
  full-text-only search and nobody is told.
- No `TEST_DATABASE_URL`, though `17 §4` makes it the guard against destroying the
  live DB.
- `/register` is "rate-limited by IP and by registration_code", but every rate limit
  in 16 is per-principal and `/register` is unauthenticated. No config knob exists.
- **`API_PROCESSES > 1` is under-specified** — the audit queue, federation catalogue,
  name mapping and pending-call state are all per-process in-memory, yet build step 15
  requires a **two-API-process** deployment as acceptance.
- `/health` has no `reviews_overdue` (required by `22 §3`), and `secrets: "unavailable"`
  isn't in the `degraded` rule, so a box with no key reports `ok`.

---

## 17 — Security guards (the strongest artefact in the corpus)

A test is not trusted until shown to fail. Each guard names file, exact source text to
remove, its replacement, and the test that must then fail. `break_the_guard.py`
mutates, runs, requires failure, reverts.

**Concrete:** `remove` must appear **exactly once**; not-found is *"an error, not a
skip"*. File byte-identical after revert. Harness refuses to start on a dirty tree.
Each case in a **fresh subprocess** so import caches can't hide a mutation.
**A skipped test counts as not-failing, therefore UNPROVEN** — the sharpest line in the
corpus; it closes the failure mode where a missing fixture reads as a clean run.
CI checks both directions. Four gates per build step.

**Guard-ID audit (all build-plan ranges resolve — no dangling references):**

| Prefix | Defined in 17 | Referenced by 18 | Status |
|---|---|---|---|
| AUTH 14, GRANT 16, JOB 26, PUB 16, CONN 17, VAULT 22, TRIG 25, OAUTH 10, MCP 11, SERVE 9, UI 4, AUDIT 6 | yes | matching ranges | OK |
| **FED 15, MEM 6, LIFE 10, TEAM 4, REV 4** | **0 — defined in 21 §8 / 22 §7 / 23 §5** | steps 12–14 | defined, **wrong doc** |

176 guards in doc 17, 39 more in 21/22/23, 215 total.

**Gaps:**
- **Doc 17's CI rule has a hole exactly where the new design is.** §3: *"every `id` in
  this document exists in `guards.yaml`"*. FED/MEM/LIFE/TEAM/REV are not in this
  document, so **39 guards — including every federation guard** — sit outside the
  automated registry check. Step 15's "no UNPROVEN entries" only catches guards
  someone remembered to add.
- **It is a specification of a registry, not a registry.** One worked entry (AUTH-001);
  175 one-line titles. Several guards have no single deletable line and cannot be
  expressed in the declared format: `JOB-011` "two workers never both run one job" (a
  concurrency property), `JOB-016` (an *ordering* guard — deleting the redirect breaks
  it for the wrong reason), `REV-002` (a property test), `CONN-001` (explicitly two
  mutations under one id), `AUTH-011` (a timing assertion).
- **No rule requiring a guard to be a single extracted call site** — the exact failure
  Datum-Sync already hit, where a guard written out at two call sites could not be
  break-tested and where one edit disarmed several guards at once. The lesson is not
  carried forward.
- No guards for `/register` rate limiting, `pending_calls` read visibility (the one
  place argument *values* are stored), federated `resources/read`, or `claim_codes`
  reuse.
- Dirty-tree refusal plus in-place mutation makes the harness unsafe to run in a
  parallel CI matrix; not stated.

---

## 19 — Reference scenarios

Ten end-to-end scenarios against a fixed cast of 8 principals and 4 connections.

**Concrete and mostly falsifiable** — better than typical spec scenarios. They name
fixtures, counts, seconds and error codes, and several assert **absence**: S2's
`tools/list` shows *no* vault tools (empty scope) and *no* `proxy_request`; S3.4 kills
the worker after the first delivery and requires the second to run and the first not to
repeat; S9.5 asserts `denied_burst` does **not** fire (three < five) — a threshold
tested in both directions, which is unusual and good.

**Gaps:**
- **Numbering is broken**: S1–S7, S9, S10, then **S8** last — and S8 isn't a scenario,
  it's a restatement of doc 17.
- **S6.3 is self-contradictory.** It asserts the sweep "leaves both directories" and in
  the same sentence states the rule as "the sweeper only deletes directories no service
  points at." After a revert the swung-away directory has no service pointing at it.
  SERVE-009 is worded the same way; a builder cannot tell which is wrong.
- **The cast is stale against 21** — `datum-main` has no `memory`/`code`/`compute`/
  `documents` blocks, yet S10.2 approves a `vm__restart_service` for it.
- **Teams never appear in the cast.** S10.1 calls `simone` a "team `geo` admin"; no
  scenario creates a team, and TEAM-001 — the strongest structural fence in the design
  — is exercised by no scenario.
- **S3.3 uses `{{ artifact_url.report_docx }}`** — `artifact_url` occurs zero times in
  `10-rest-api.md`. TRIG-004 tests a template namespace nobody enumerated.
- **S9.1 is protocol-confused** — the agent has "one MCP URL with a registration code"
  then "calls `POST /register`", a REST route absent from `10 §1`'s public-path list.
  The headline "one URL" claim does not survive its own scenario.
- **S9.6 contradicts `22 §1`** on what a restricted agent can list.
- Memory (step 12) has no dedicated scenario. S7 requires fault injection with no
  harness specified. S4.10 asserts what "would" happen — untestable as written.

---

## 21 — Federation and core resources (the thesis, and the thinnest engineering)

An agent is barebones alone; the gateway makes it an org member by exposing four core
resources through one MCP endpoint — **Memory** (built in) and **Code/Compute/Documents**
(federated upstream via a new `mcp` connection type), with every forwarded call checked
down to its **arguments** by declarative guards.

**Concrete:** `mcp` connection config (`url`, `tool_prefix`, `resource_kind`,
`auth_inject`, `refresh_seconds: 300`, `timeout_seconds: 60`); private origins allowed
for `mcp` (LAN company servers), tier-5 to create. Catalogue federation in 5 steps;
**down upstream = omitted + `federate.unavailable` audit row, not an error**;
`tools/list` never blocks >**5 s** per upstream. Call path pseudocode orders
`audit.write_sync('federate.call')` **before forwarding**. *"Notifications, prompts and
sampling are not federated: the gateway is a tool and resource broker, not a full MCP
relay"* — good scoping. Memory DDL is real (CHECK constraints, generated tsvector,
named indexes, UNIQUE, `expect_version` → 409). Five profiles shipped as **data, not
code**. Narrowing rule worth keeping: *"`commands.allow` ⊆ parent's **by string
equality** — regexes are not subsumed"*; correct, since regex subsumption is undecidable.

**Argument guards — model right, grammar insufficient.**

Handled well: `default: deny`, and an unrecognised upstream tool *"is not even
listed"* (FED-003). *"A guard whose `args` name an argument the call did not supply →
deny (a missing repo is not 'any repo')"* — fail-closed on the case that usually leaks.
Guard-vs-`inputSchema` mismatch raises `federate.guard_mismatch`. Compute is
deny-then-allow (FED-008), the correct order.

Blocking a builder:
1. **Composite arguments are unexpressible, and the flagship profile needs them.**
   `argument_path` is a single dotted path. `github-mcp` maps `owner/repo` → `repos`
   against `repos: ["datum/*"]`, but GitHub's tools take `owner` and `repo` as
   **separate** arguments. The worked example compares `"gateway"` against `"datum/*"`
   and **denies everything**. No syntax for joining two arguments.
2. **`paths` is not a valid grant field** — valid targets are `repos, hosts, folders,
   paths.read, paths.write`, yet `ssh-mcp` writes `path` → `paths`, and nothing decides
   read vs write globs.
3. **Value types beyond string/list-of-string undefined** — GitHub's `push_files` takes
   an array of objects each carrying `path`; a dotted path cannot index list elements.
4. **Where the name→(conn, tool) mapping lives is never stated.** §2.3 opens with
   `mapping[name]`; with `API_PROCESSES > 1` a `tools/call` can land on a process that
   has never run `tools/list`.
5. **`tools: {allow, deny}` exists only on the `code` block**, yet FED-002 tests it for
   all kinds.
6. **`resources/list`/`resources/read` federate with no guards at all.** Argument guards
   are defined only over `tools/call`. A federated Drive or code server exposing file
   content as *resources* is reachable, prefix-mapped, with **no folder or repo check**.
   This is a security hole in the design, not a documentation gap.
7. **Prefix collisions unhandled** — nothing resolves a repository named `gh` against a
   connection with `tool_prefix: gh`.

**Other gaps:** `memory_delete` is specified as a soft delete against a table with **no
tombstone column**, and `memory_history` has no FK — a hard delete orphans history.
Build step 12 requires "search ranks title matches above body" against an **unweighted**
tsvector (needs `setweight`). `embedding VECTOR(1536)` hardcodes an OpenAI dimension
while the embedder is configurable. `pending_calls` has `status='expired'` with **no
expiry column and no TTL**; `requested_by`/`approved_by` are bare `INTEGER` with no
`REFERENCES`. `pending_calls.args` storing values contradicts `14 §1`'s "never stored:
parameter values"; 14 is never amended and no guard covers who may read it.
**`narrows()`/`intersect()` in `03 §5` cover none of the new blocks** — no meet defined
for `code.repos`, `compute.commands` (regex lists), `documents.folders` or memory globs,
yet GRANT-010 and FED-015 depend on it. `11-mcp.md` has **zero** occurrences of
"memory" or "federat".

---

## 22 — Agent lifecycle

Six-state machine on `principals.state` replacing the `disabled` boolean; two
registration paths; review obligation with inactivity demotion; reversible restriction
preserving the prior grant; retirement purging credentials but keeping history.

**Concrete:** `pending → active → restricted → active`, `active → disabled → retired`,
`pending → rejected`. Only `active` authenticates normally; `restricted` authenticates
**forced to tier 1**; the other four → 401 **with the state as the error code**.
Migration adds `state`, `restricted_from JSONB` (the grant before restriction, for
exact restore), `review_due_at`, `metadata JSONB`; drops `disabled`.
`registration_codes` holds a code **hash**, `max_uses`, `template_grant`,
`auto_approve`; `requested_grant` must narrow the template. Tier ≥3 may sponsor agents
≤ tier 2; tier ≥4 otherwise. MCP `delegate_create` auto-approves within the sponsor's
own envelope — correct. Claim within **24 h**, single-use. `policy.lifecycle`:
review 90 d, inactivity restrict 30 d, disable 90 d, pending TTL 7 d.
Restriction keeps tokens alive at tier 1 *"so the agent can still `whoami` and read its
own namespace — it can see **why** it was restricted via `memory_get
agents/{name}/_status`"* — a genuinely good instinct; a revoked agent that can discover
its own state won't retry-loop blindly. `whoami` returns a computed `capabilities`
summary *"so an agent can describe what it can do without parsing the grant."*

**Gaps:**
- **The migration violates the corpus's own two-release rule.** `16 §9` requires
  add-and-backfill then drop; `011_lifecycle.sql` does `ADD COLUMN state` and
  `DROP COLUMN disabled` in one file, with an inline comment acknowledging the rule.
- **No `disabled → active` edge**, yet `16 §6` ships `principals disable|enable` and
  `14 §3` has `principal.enable`. No `restricted → disabled`/`retired` either, so a
  restricted agent can't be retired without first restoring full authority.
- **`memory.write` is missing from the restriction list**, and §4 requires the gateway
  to *write* `agents/{name}/_status` without saying under which principal or how that
  escapes the restriction it announces.
- **`claim_codes` has no columns anywhere in the corpus** (one mention, in `04`'s
  migration index). `registration_codes`' columns exist only as prose, in a corpus whose
  stated convention is "column types are explicit; every table has a comment block".
- **None of this document's routes exist in `10-rest-api.md`** — zero hits for
  "register" or "review". `/register` must be public and is not in `10 §1`'s public-path
  list; with "auth: fail closed" the route as specified is **unreachable**.
- `review_grace_days` is referenced in §3 and absent from the `policy.lifecycle` block
  printed in the same section.
- **LIFE-010 can conflict with `03 §5.3`** — if a parent narrowed while a child was
  restricted, restoring the child's exact stored grant reinstates one that no longer
  narrows its parent. No rule resolves it.
- No rule for whether `metadata` (model, host, owner contact) is attacker-controlled at
  self-registration. It is, by construction — `POST /register` is public.

---

## 23 — Tenancy and activity review

Teams as a labelling and fencing dimension over one shared gateway — explicitly not
separate databases, processes or URLs — plus rollups, seven anomaly signals, one review
inbox, monthly per-team exports.

**Concrete:** `teams` with `vault_root`, `memory_root`; `team_id` on `principals`,
`connections`, `repositories`, `hosted_services`, all `ON DELETE SET NULL`.
Strongest rule in the document: *"Connections with a `team_id` are usable only by
principals of that team, **in addition to** the grant's own lists — a second,
structural fence that a grant typo cannot cross"* (TEAM-001). One team per principal;
children inherit and cannot leave. `vault_root`/`memory_root` are UI defaults,
explicitly *"not themselves permissions"*. §2 states the single-tenancy reason plainly:
*"the question 'what did every agent in the company do yesterday' must have one answer."*
Seven signals with numeric rules: `denied_burst` ≥5 on one `target_kind` within **10
minutes**; `off_hours` >20%; `volume_spike` >**3×** trailing 14-day median;
`approval_pressure` >3 pending/day. *"Hints for a human, never an automatic
restriction"*, with one policy-listed exception. §3.4's trace example (Drive search → 3
gets → `memory_put` → `gh__push_files` **denied** → `memory_put _last_error`) is the
best illustration in the corpus.

**Gaps:**
- **`denied_burst` cannot be computed from what the document says computes it.** §3.1
  establishes **daily** rollups "so a 90-day summary does not scan the spine"; §3.2 says
  the daily tick flags signals *from rollups*; `denied_burst` needs ≥5 events **within
  10 minutes**. Daily rollups have no sub-day resolution. REV-002 doesn't catch this,
  and `19 S9.5` asserts the signal's behaviour as acceptance.
- **`audit_rollups` and `review_signals` have no DDL** — inline column lists in prose
  only. No types, keys, indexes or uniqueness, and `audit_rollups` needs a composite PK
  to be idempotent under a re-run daily tick.
- **`by_resource` requires an audit `target` grammar `14` does not define**, and
  `14 §1` + AUDIT-004 forbid the parameter values `21` puts in `args_summary`. Three
  documents, three positions.
- **`new_resource` has no mechanism** — needs an unbounded scan or a seen-set table,
  neither specified; `audit_days: 365` degrades "first ever" into "first this year".
- **`volume_spike` has no floor** — 1 → 4 requests fires at 3× median, so every
  low-traffic agent alarms.
- **`policy.review.working_hours`, `max_children_per_day`, `auto_restrict_on` are absent
  from `16 §2`**, and `auto_restrict_on` has no value grammar.
- **Team fencing and audit visibility are unreconciled** — a tier-4 in team `geo` gets
  one answer from `/rest/v1/audit` and another from `/activity`.
- The `team_id IS NULL` **principal** case (marcus, `system:worker`, any pre-teams
  agent) using a team connection is undefined, as is TEAM-002's inheritance from a
  NULL-team parent.
- `GET /rest/v1/review/report` has no output schema; `19 S10.4` accepts on "produces the
  summary her manager asked for", which is not a test. Route absent from `10`.
- §4 adds six UI screens to a document (15) that does not list them.

---

## Assessment

**01–20 is a spec. 21–23 is a proposal with a schema sketch.**

Real DDL with real constraints; real numbers everywhere (10 000-deep queue, 200-row
batches, 2 KiB cap, 5 s federation timeout, 300 s TTL, 45 s MCP wait, 72 h promotion
TTL, ≥5-in-10-min, 3× median, 24 h claim); fail-closed defaults stated as defaults;
explicit non-goals; decisions carrying reasons. The guard/mutation harness is the best
idea in the corpus and encodes a lesson most specs never learn.

The defects are overwhelmingly of one kind: **a strong later idea bolted on without a
pass back through the documents that own its artefacts.** Documents 21–23 introduce
8 tables, ~20 routes, 6 UI screens, 3 policy blocks, ~15 audit verbs and 39 guards, and
exactly one of those integrations was done (`04`'s migration index). It reads as
complete because each document is internally tidy.

**Buildable:** steps 1–11.
**Needs design work first:** step 12 (soft-delete and ranking contradictions),
step 13 (argument-guard grammar, resource federation guards, mapping persistence —
the composite-argument problem alone blocks the GitHub profile),
step 14 (DDL for four tables, schemas for ~15 routes, three policy blocks, the
rollup-granularity contradiction).

The README's instruction to *"read 21–23 immediately after 01"* is correct about where
the vision is, and is also an accurate — if unintended — warning about where the
specification thins out.
