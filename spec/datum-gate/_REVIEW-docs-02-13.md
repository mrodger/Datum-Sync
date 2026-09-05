I have read all thirteen documents plus the supporting docs they cross-reference (00, 14, 16, 17, 21). Report below.

---

# Datum-Gate spec review — documents 02–13

---

## 02 — Domain Model

**What it specifies.** The noun set and their relationships: a principal/credential/grant authority tree; a repository → workspace → workspace_version catalogue where versions are immutable and content-hashed; jobs that pin a version and freeze a grant; and the peripheral entities (connection, schedule, automation, delivery/outbox, hosted service, promotion, worker lease registry, audit log). It also fixes naming regexes and the delete-cascade matrix for every entity. It is a glossary plus two normative tables, not a mechanism doc.

**Concrete details worth knowing.**
- Naming regexes are normative here and referenced by every other doc: principal `^[a-z][a-z0-9._-]{1,62}$`; repository/workspace `^[A-Za-z][A-Za-z0-9_-]{0,63}$` and **must equal the directory name and `manifest.name`**; connection `^[A-Za-z][A-Za-z0-9_.-]{0,63}$` globally unique; hosted-service name `^[a-z0-9][a-z0-9-]{0,62}$` globally unique *because it is a URL segment*; MCP tool name `{repository}__{workspace}` ≤128 chars; vault path relative, `/`-separated, no `.`/`..`, ≤1024 chars.
- Trace id is server-minted; a client-supplied `X-Trace-Id` is stored separately as `client_trace_id` and **never trusted**. This distinction is carried consistently through 04, 10, 11 and 14.
- Cascade table: deleting a workspace version is refused if it is current or pinned by a non-terminal job; deleting a job does not remove artifacts (only the retention sweeper does); deleting a connection is refused if any current version declares it; hosted services get `source_job` set NULL, not removed.

**Contradictions and gaps.**
1. **Principal name regex is wrong here.** 02 says `^[a-z][a-z0-9._-]{1,62}$` (no colon). 04's DDL says `^[a-z][a-z0-9._:-]{1,62}$` and the seed rows are literally `system:worker` and `system:local`. As written, 02's rule forbids the two principals the migration inserts.
2. **Principal deletion contradicts 04 and 10.** 02 says deleting a principal *disables* its children. 04 declares `parent_id ... ON DELETE RESTRICT`, and 10 §8 says `DELETE /rest/v1/principals/{name}` returns **409 if it has children (disable them first)**. Three documents, two incompatible behaviours.
3. 02 says jobs/audit rows keep "a dangling id"; 04 declares `jobs.principal_id ... ON DELETE SET NULL`, so the id is nulled, not dangling. (`audit_log` genuinely has no FK, so 02 is right about audit only.)
4. **"Refused if any job pins it and is not terminal"** for workspace-version deletion describes an operation with **no surface anywhere in 10**. There is no delete-version route or CLI verb in the corpus.

---

## 03 — Authority

**What it specifies.** The single permission model. A `Grant` JSON document on every principal carrying tier (1–5), repository scope, connection use/proxy lists, vault glob scopes, per-block federated-resource scopes, and rate limits. Delegation is a tree with a `narrows(child, parent)` predicate checked at write time and an `intersect` over ancestors recomputed at authentication so that later narrowing propagates without rewriting descendants. Jobs freeze a copy of the effective grant. It also gives the credential resolver, glob subsumption algorithm, rate limiting, and password lockout.

**Concrete details worth knowing.**
- **Tier verb ladder is enumerated, cumulative**: 1 observer (whoami, list, vault read/list), 2 reader (read jobs/logs/artifacts, `resources/read`, quarantine writes), 3 operator (submit/cancel/resubmit, `/stream` `/download` `/upload`, `tools/call`, vault write, credential proxy ≤ tier 3, create schedules/automations, *request* promotions), 4 admin (publish, create principals tier ≤4 with grant ⊆ own, manage connections ≤ tier 4, approve promotions, register proxy services), 5 superuser.
- **Ten grant coherence rules**, validated on every write. Notable: `admin == (tier >= 4)` is derived and `admin:true` with tier<4 is rejected; `connections.proxy` may not contain `*` (proxy rights are enumerated on purpose, unlike `connections.use`); write-implies-read is checked *by pattern subsumption at write time* and deliberately **not** inferred at decision time ("inferring it at decision time would let the validator be deleted with every test passing"); promote rights are stored as **edges** (`"src_glob -> dst_glob"`), not two independent lists.
- `narrows()` is enumerated field-by-field: tier ≤, repositories ⊆ (`["*"]` ⊆ only `["*"]`), connections.use ⊆, connections.proxy ⊆, per-action vault subsumption, promote edges subsumed at both ends, **deny compared in the reverse direction** (every parent deny must be subsumed by some child deny), every limit ≤.
- `subsumes(outer, inner)` is given as actual pseudo-code with a `**` backtracking loop. `intersect_patterns(a,b)` is explicitly **conservative** — it keeps only patterns where one side subsumes the other and drops merely-overlapping pairs, which is the safe direction for authority.
- Resolver: token-shaped credentials are 32 random URL-safe bytes stored as `sha256(raw)` (justified: no dictionary, hot path); passwords argon2id. Unknown/revoked/expired are **all 401** but with distinguishing `error.code` — with a stated rationale (a real client needs to know to refresh; an attacker learns nothing timing-wise).
- `DATUM_GATE_AUTH=off` dev bypass requires **both** loopback bind *and* loopback peer socket, because `uvicorn --host` bypasses the config value.
- Lockout: keyed on **submitted principal name**, checked **before the DB is consulted**, 5 failures / 15 min, `PASSWORD_MAX_CONCURRENT=4` semaphore, unknown name still pays a full argon2 hash.

**Contradictions and gaps.**
1. **`narrows()` and `intersect()` ignore five of the grant's own blocks.** §3 defines `memory`, `code`, `compute`, `documents`, `mcp` blocks; §5's narrowing algorithm and §5.1's intersect enumerate only tier/repositories/connections/vault/limits. 21 registers guard **FED-015** ("child code/compute/documents blocks must narrow the parent's") — so the guard registry asserts a property the normative algorithm in the load-bearing document does not implement. Likewise §3's ten coherence rules validate nothing about those blocks.
2. **`subsumes()` pseudo-code has a real bug and a dead branch.** The line `if O[i] contains '?' : compare literally against a non-wild inner segment` never returns; control falls to `return False`. And "compare literally" is wrong — `a?c` should subsume `abc`. Since `?` is the one wildcard 08 §2 permits inside a literal segment, this is a live case.
3. **`compute_effective` runs an ancestor walk on every authenticated request**, plus two `UPDATE`s (`credentials.last_used_at`, `principals.last_seen_at`). No caching, no invalidation strategy, no batching is specified. Every GET becomes a write transaction. Not addressed anywhere in the corpus.
4. Env var name is `DATUM_GATE_AUTH` here and `DG_AUTH` in 16 §1. Policy key is `policy.max_delegation_depth` here and `policy.delegation.max_depth` in 16 §2. §8 names `WORKER_COUNT`/`API_COUNT`; 16 defines `API_PROCESSES`. Three separate naming collisions with the config doc.
5. §8 describes a **sliding window** in memory but a `rate_windows(principal_id, window_start, count)` table for multi-process — that table shape is a fixed window. The two modes have different semantics, and no retention/cleanup for `rate_windows` is specified anywhere.

---

## 04 — Schema

**What it specifies.** The complete final-state DDL for nine migration files, with checksum-guarded migrations (`schema_migrations(filename, checksum, applied_at)`, refuses to re-run a file whose checksum changed). Constraints are explicitly framed as the invariants the code depends on. Includes the two seeded system principals and an asyncpg JSON codec requirement.

**Concrete details worth knowing.**
- `jobs`: `CHECK ((status = 'running') = (lease_worker IS NOT NULL))` — the lease invariant is enforced by the database, not just by code. Partial indexes are chosen for the real access paths: `jobs_queue_idx (priority DESC, submitted_at) WHERE status='queued'`, `jobs_running_idx (lease_until) WHERE status='running'`, `jobs_pending_automations_idx (completed_at) WHERE automations_at IS NULL AND completed_at IS NOT NULL`.
- `triggered_by` is regex-constrained: `^(schedule|automation|resubmit|mcp|rest|upload|stream|download):`.
- `credentials`: `CHECK ((kind LIKE 'oauth_%') = (client_id IS NOT NULL))`; unique partial index `credentials_one_password_idx ON (principal_id) WHERE kind='password' AND revoked_at IS NULL` — one live password enforced structurally.
- `connections`: `CONSTRAINT connections_scope_targets` forces `global ⇔ cardinality(scope_targets)=0`; `CHECK ((secret IS NULL) = (secret_key_id IS NULL))`; secret layout is `key_id || nonce || ct || tag` in a `BYTEA`.
- `hosted_services`: `CHECK ((kind='static' AND path/repository/workspace NOT NULL) OR (kind='proxy' AND origin_url NOT NULL))` and `CHECK ((visibility='tier') = (min_tier IS NOT NULL))`.
- `deliveries.dedupe_key TEXT NOT NULL UNIQUE` — globally unique, which is what makes the outbox insert idempotent.
- `oauth_codes.used_at` exists **so replay is detectable — "no DELETE"**.
- `audit_log` deliberately has **no FK on actor_id** so rows outlive principals.
- The asyncpg codec note is a genuinely useful defect-class elimination: register `json`/`jsonb` codecs in the pool `init=` hook so no module receives a string where it expects a dict.

**Contradictions and gaps.**
1. **`jobs.max_attempts SMALLINT NOT NULL DEFAULT 2`** vs 05 §2.5's manifest `max_attempts` **default 1, max 5**. Nothing in 06 §2 (submit) says the manifest value is copied onto the row. An implementer following 06 literally gets DB-default 2 for every job regardless of the manifest, silently doubling retry behaviour for a workspace that declared 1.
2. **`oauth_codes.code` is stored in plaintext** while every other credential in the system is hashed. A read of that table yields usable authorization codes for their 10-minute window. Inconsistent with the corpus's own stated posture; not called out.
3. `schedules.timezone TEXT NOT NULL` with no default and no validity check; 16 supplies `DEFAULT_TIMEZONE` but the DDL will reject a NULL that a partial `PATCH` might produce.
4. The "Later migrations" table forward-references `010_memory.sql`, `010b_federation.sql`, `011_lifecycle.sql`, `012_teams.sql` from documents 21–23 — including `connections.type` gaining `'mcp'`, which means the `CHECK (type IN (...))` in `005_connections.sql` is known-incomplete at the moment it is written. Fine, but the base `type` CHECK must be dropped and recreated, which the table does not say.
5. `rate_windows` and `webhook_receipts` have no retention sweep. 06 §12 sweeps artifacts, job_log and uploads; nothing sweeps these two ever-growing tables.

---

## 05 — Workspace Contract

**What it specifies.** The on-disk shape of a workspace (`main.py`, `manifest.json`, `MANIFEST.md`, optional `requirements.txt`/`.dgignore`), the full manifest schema including eight parameter types and their coercion, the `async def run(params, emit, connections)` interface with four event types, a ten-check publish gate that runs cheapest-first, the content-hash algorithm, per-workspace venv construction, and the subprocess isolation envelope.

**Concrete details worth knowing.**
- **Content hash algorithm is given literally**: `sha256` over, in sorted path order, `relative_path + "\0" + file_size + "\0" + sha256(file_bytes) + "\n"`. `requirements.txt` included; `__pycache__`, `.venv`, `*.pyc` always excluded. This is what makes `sync` idempotent.
- **Publish gate order matters and is specified**: (1) files present, (2) manifest validates + name matches directory, (3) `main.py` **parsed with `ast`, never imported**, (4) five non-empty MANIFEST.md sections — where "non-empty" explicitly excludes `N/A`, `TBD`, `TODO`, dashes, ellipses and HTML comments, (5) content hash unchanged ⇒ skip, (6) connections exist/scope/access/tier ≤ publisher/in publisher's `connections.use`, (7) service name collision, (8) `mcp` ⇒ no required `FILE`, (9) venv build, (10) smoke test ≤30 s. Commit is one transaction: insert version, mark previous `superseded`, move `current_version_id`, audit.
- Venv path is `DATA_PATH/venvs/{repo}/{ws}/{sha256(requirements.txt)[:16]}`, built with `--system-site-packages`, recorded as `manifest._venv`.
- Isolation envelope is enumerated: env is **`PATH HOME LANG LC_ALL TZ SSL_CERT_FILE` only** plus `PYTHONUNBUFFERED`, `PYTHONPATH=<child harness dir only>`, `DG_JOB_ID`, `DG_TRACE_ID`; no `DATABASE_URL`, no secret key, no `.env`; own session/process group; `PR_SET_PDEATHSIG=SIGKILL`; **spec on stdin, never argv**.
- `GEOMETRY` parameters are validated at submit with PostGIS `ST_GeomFromText`/`ST_GeomFromGeoJSON` — described as "the one PostGIS use in the gateway", which explains why the extension is required at all.
- Required parameters MUST NOT declare a default; unknown manifest keys refused except top-level `x-*` (the workflow-builder seam).

**Contradictions and gaps.**
1. **Version rollback is unbuildable as specified.** 02 calls versions "immutable, hash-addressed" and 10 §5 offers `POST .../versions/{id}/activate` — but that route requires "its content hash still matches disk". **Nothing in the corpus stores the workspace bytes.** Only the hash is kept. So activating an older version works only if you have already reverted the working tree by hand, at which point `sync` would do it. The rollback feature is effectively inert and the "immutable version" framing is misleading.
2. **Check 5 short-circuits checks 6–10.** If a connection is later deleted, re-tiered, or removed from the publisher's `connections.use`, re-running `sync` reports `unchanged` and never re-validates. Combined with 07 §3's "tier is not re-checked at job run time", a workspace can run indefinitely against a connection whose tier gate no longer holds. `--force` exists but nothing requires it.
3. `manifest._venv` is written by the gate into a document whose own rule (§2) says unknown keys are refused and (§9) says only `x-*` is permitted. PUB-016 guards *user* manifests, but the doc never states that validation happens before injection and re-validation must skip it.
4. §8 says a sandbox wrapper is optional and **"no sandbox is the default"**. That single line undermines 08 §9's claim about job vault confinement (see below).

---

## 06 — Job Engine

**What it specifies.** The full lifecycle: submit with idempotency and admission control, `FOR UPDATE SKIP LOCKED` claim, leases with heartbeats and a reaper, the worker poll loop, the runner/child NDJSON protocol, artifact reconciliation, finish, cancel, resubmit, SSE streaming, and the synchronous execution helper that the `/stream` and MCP surfaces sit on. Ends with six invariants the tests must prove.

**Concrete details worth knowing.**
- **Claim SQL is given verbatim** with `FOR UPDATE SKIP LOCKED LIMIT 1`, ordered `priority DESC, submitted_at`, incrementing `attempt` and setting the lease in the same statement.
- **Reaper SQL is given verbatim**, two statements: requeue where `lease_until < now() AND attempt < max_attempts`; fail with `'worker lease expired after N attempt(s)'` where `attempt >= max_attempts`. Guarded by a short advisory lock so only one worker reaps. `DELETE FROM workers WHERE heartbeat_at < now() - interval '5 minutes'`.
- Heartbeat interval is `JOB_LEASE_SECONDS/6` (10 s at default 60), and **a heartbeat that affects 0 rows means the worker lost the job**: it kills the child and writes no terminal status. `finish` is `WHERE id=$1 AND status='running' AND lease_worker=$5` — zero rows means reaped, log and move on.
- **The child's fd juggling is correct and specified**: `event_fd = os.dup(1); os.dup2(2, 1); sys.stdout = sys.stderr` **before any workspace code runs**, so `print()` cannot corrupt the event channel. `main.py` loaded by `importlib.util.spec_from_file_location`, never via `sys.path`.
- **Reconciliation rules**: undeclared artifact ⇒ job failed; `service/*` must be a directory and non-service must not be; a **missing primary output fails the job** with the stated reason that "a `/stream` caller would otherwise get 200 with nothing"; `type` and `primary` come from the manifest, never from the child.
- Cancel of a running job is `NOTIFY jobs_control` to the leasing worker; the API returns `{"result":"signalled"}` either way, deliberately not pretending to be synchronous.
- **Resubmit uses the current version and the caller's grant**, never the original's frozen one — a deliberate and correct choice, and guarded (JOB-024).
- SSE ordering is specified: `LISTEN jobs` **first**, then the status frame from the row, then replay `job_log` after `Last-Event-ID`. Status frames are always rebuilt by re-reading the row so "one event name has one shape".
- `run_sync` refuses `503 NO_WORKER` if no worker has heartbeat in 30 s — which is what stops `/stream` from hanging for its full timeout on a dead cluster.

**Contradictions and gaps.**
1. **Where connections are resolved is ambiguous.** 07 §5 says an out-of-scope connection "fails the job at claim time, before the child is spawned"; 06 §3's claim SQL and §6's runner signature both take `connections` as already-resolved input, and no step in 06 calls `resolve()`. An implementer has to guess whether resolution failure produces a `failed` job (07's claim) or an exception in the claim transaction (which would roll back the claim and hot-loop the job).
2. **What happens when no secret key is configured and a job needs a connection secret is unspecified.** 07 §4 covers writes (503) and startup (succeeds); the run path is silent.
3. `max_attempts` is never copied from the manifest to the row (see 04).
4. §5 lists `--tags` as "accepted and ignored in this build" but the `jobs` table has no `tags` column, so even the routing seam is absent — harmless but the doc implies more than exists.
5. `concurrent_jobs` is explicitly *not* re-checked at claim, only at submit. Stated, but it means a burst submitted while jobs were completing can exceed the limit in practice.

---

## 07 — Connections and the Credential Proxy

**What it specifies.** A two-halved credential store — a readable `config` JSONB and a sealed `secret` BYTEA that no read path ever returns — with seven connection types, AES-256-GCM sealing keyed from the environment with rotation support, a tier/scope/access model, resolution into the dict a workspace receives, and the credential proxy that lets an agent use a key it must never hold.

**Concrete details worth knowing.**
- **The secret is hidden structurally, not by stripping.** "Every read SQL uses one `_COLUMNS` string that excludes `secret`. The only function that reads the column is `_sealed()`, called by `resolve()`, `test()` and `proxy.forward()` — none of which return to an HTTP client." This is the right pattern: no route can leak it by forgetting.
- The store **refuses secret-shaped key names in `config`** with an enumerated blocklist: `password passwd pass secret token access_token refresh_token client_secret api_key apikey private_key passphrase credentials key secret_key`.
- **Sealed layout**: byte 0 = `key_id` (1..255), bytes 1..12 = 96-bit nonce, remainder = ciphertext+16-byte tag. **AAD = the connection name**, so moving a blob between rows fails to open — this is what prevents one `UPDATE` from relocating a tier-5 password onto a low-tier connection. Rotation is: add `DG_SECRET_KEY_2`, set `DG_ACTIVE_KEY_ID=2`, run `keys reseal`, remove key 1. `open_()` returns **one** error for wrong key / tampered / wrong AAD.
- Each type declares required config, typical secret keys, and a real tester: `database` → `SELECT 1` (10 s), `http` → `GET base_url` with 401/403 counting as failure, `smtp` → EHLO+AUTH **no send**, `imap` → LOGIN+SELECT, `s3` → HEAD bucket, `oauth_client` → client-credentials fetch.
- `auth_inject` has four concrete shapes (bearer / basic / header / query) with the field names given.
- **Proxy check order is enumerated** (1 exists+is http, 2 in `connections.proxy`, 3 tier ≥ connection tier, 4 method allowlist, 5 URL join with `://` and `..` refused, 6 SSRF, 7 inject, 8 redirects, 9 cap, 10 audit). Caller-supplied `Authorization`, `Cookie`, and any header named in `auth_inject` are dropped before injection. Max 5 redirect hops, re-checked per hop, injected auth stripped cross-origin, 301/302/303 converted to GET. Response cap 1 MiB with a truncated flag; `isError` at status ≥400. **Bodies are never stored in audit.**
- `allow_private_origin: true` is the escape hatch, settable only by a tier-5 principal, and every audit row for it is flagged `governance: true`.

**Contradictions and gaps.**
1. **DNS rebinding is not addressed.** Step 6 resolves the host and checks every A/AAAA answer, then step 7+ hands the URL to httpx, which resolves again. CONN-009/010 guard the check itself, not the TOCTOU. For a doc this careful about egress, the omission is conspicuous.
2. **Tier is checked at publish and never again** (§3), and 05's check-5 short circuit means publish may never re-run. The combination is a permanently stale authority check; neither doc mentions the other's contribution.
3. A tier-3 principal can submit a job to any workspace in scope, and that workspace runs with **whatever connections an admin published it against**, including tier-5 ones. This is stated as intent ("a workspace runs with its own authority, chosen once by whoever published it") but it means the connection tier ladder does not constrain job submitters at all — only publishers and proxy callers.
4. `config.allow_private_origin` is a config key with authority semantics living in a JSONB blob with no CHECK; the "only tier 5 can set it" rule is code-only (guarded as CONN-015, so at least it is registered).

---

## 08 — Vault Gate

**What it specifies.** Scoped filesystem access to `VAULT_PATH`: path normalisation that refuses rather than resolves traversal, a four-token glob language, a deny-first decision function, six write-time coherence rules, nine operations with size caps, and the two-phase quarantine→promotion flow with hash re-checking.

**Concrete details worth knowing.**
- **Traversal is refused, not resolved** — "a path that needs resolving is a 400, because the written form is what the scope is checked against". Then, additionally, `resolved.is_relative_to(VAULT_PATH.resolve())` to defeat symlinks; a symlink pointing out is treated as not existing.
- Glob language is exactly four tokens; **partial wildcards (`*foo`, `f*o`) are refused at grant validation** with the honest reason "the engine handles them correctly but operators do not expect them". Patterns compiled once with an LRU cache, anchored both ends.
- **`list` on a directory nothing could match returns 403, not an empty list — "an empty list leaks existence"**. Refused paths are 403 and never 404.
- **Write does not imply read at decision time** even though the validator requires write ⊆ read at write time, with the stated reason that inferring it "would let the validator be deleted with every test passing". That is a genuinely sophisticated argument about testability of a guard.
- Concrete caps: read 256 KiB with `truncated`+`size` and offset/limit paging; write 4 MiB; list depth 1..3 and ≤2000 entries; search ≤200 results with a 5 s budget.
- Writes are temp-file + `os.replace` (atomic) and take `pg_advisory_xact_lock(hashtext(path))` so two gateway processes cannot interleave a write and a promote on one path.
- Promotion: content hash captured at request, **re-hashed at approval and rejected with `'source changed since request'` on mismatch**; approver must be a different principal below `self_approve_tier` (5) and **must also hold the same promote edge**; pending rows expire after `pending_ttl_hours` (72). Quarantine writes are create-only and drop a `path.dgq.json` sidecar recording `{written_by, trace_id, sha256, at}`.
- **Two independent gates on governance paths**: the grant says where you may write; `policy.governance_write_min_tier` (default 4) says what is protected, enforced regardless of grant, checked in that order, both audited on refusal.
- The `external` search backend must have its results **re-filtered by the caller's read scope**, "so an index that knows more than the caller may see cannot leak through it". This is the correct way to federate a search index.

**Contradictions and gaps.**
1. **§9 is the biggest hole in the corpus.** It claims "A drone's job therefore cannot reach beyond the slice its dispatcher gave it" — but enforcement is `datumgate_child.clients.vault(conn_obj)`, a **helper inside the child process**. The workspace is arbitrary Python that receives `{"root": "/vault/..."}` in a dict and can call `open()`, `pathlib`, or `subprocess` directly. 05 §8 confirms **no sandbox by default**. VAULT-021 ("job vault access enforced against the frozen grant") will pass against the helper while the property it names is false. This is a guard that proves the wrong thing, in a document set whose entire premise is that guards must be falsifiable.
2. §4 rule 4 allows a promote edge whose source is covered by `quarantine` **or** `write`, but `_apply` *deletes* the source. A principal with quarantine-but-not-write on the source therefore performs a delete it has no `write` right for. Small, but the deny/allow model is otherwise strict about exactly this.
3. The `.dgq.json` sidecar is a file inside the vault. Whether it appears in `list`, whether reading it needs `read` scope, and whether a caller can write one directly to forge provenance is entirely unspecified.
4. `search` is listed under grant action `read` but its cost model (5 s budget, full walk of readable roots) has no rate limit of its own beyond `calls_per_minute`.

---

## 09 — Schedules and Automations

**What it specifies.** Two worker-driven mechanisms with no state outside Postgres. Schedules convert time to jobs via a single `next_run` column. Automations are YAML documents (one trigger, ordered actions) that fire into an outbox: one `automation_runs` row plus one `deliveries` row per action, at-least-once with a unique dedupe key and exponential backoff. Includes a closed template language and loop prevention.

**Concrete details worth knowing.**
- Cron evaluated in the schedule's timezone then converted to UTC ("so 07:00 every weekday survives DST"); intervals are durations and do not shift; **a missed schedule fires once**, walking the due time forward past now while preserving an interval's phase; re-enable re-times from now.
- Four trigger types with real specifications: `job_complete` (absent repository means **the owner's repositories, never "all"**); `schedule`; `webhook` (`X-DG-Signature: sha256=<hmac>` over the **raw body** plus a fresh `X-DG-Nonce`, 10-minute replay window in `webhook_receipts`); `email` (IMAP poll ≥60 s, UIDs recorded in a **bounded ring of 5000** in `config._seen_uids`).
- **Loop prevention is two mechanisms**: `triggered_by = "automation:{name}"` skips self-caused jobs, and a walk up the `parent_job` chain to `max_chain_depth` (8) refusing if the same automation name appears anywhere — explicitly "closes the A→B→C→A gap the original documented as known". Plus `automations.created_at <= job.completed_at` so a pre-existing job is never delivered.
- **Template language is deliberately crippled**: `{{ name }}` over a fixed namespace (`job.* params.* artifact_url.* metrics.* trigger.* automation.* now`), dict lookup only, **no attribute traversal, no filters, no expression evaluation**, unknown placeholders refused at save, absent `params.X` renders `""`. The rendered namespace is stored on `automation_runs.context` so a delivery is reproducible.
- **Frozen `owner_grant` at save**; actions run under `intersect(owner.effective_now, owner_grant)` — "a later widening of the owner does not widen an automation written earlier; a narrowing applies immediately".
- `vault_write.path` is checked at save against the **literal prefix before the first `{{`**, and again at fire time on the rendered path.
- Backoff formula is given: `now() + min(2^attempts * 30s, 1h)`, `max_attempts` 5, then `status='dead'`. Per-kind idempotency is named for each: `run_workspace` uses the dedupe key as `Idempotency-Key`; `http_request` sends `X-DG-Delivery-Id` and the doc **says plainly that HTTP is not idempotent**; `email` derives `Message-ID` from the dedupe key; `vault_write` is naturally idempotent.

**Contradictions and gaps.**
1. **§5 contradicts itself in one paragraph.** "A crash between 1 and 2 loses consideration of that job" then "steps 1–2 are one transaction". If they are one transaction, a crash rolls back the `automations_at` claim and nothing is lost. An implementer cannot tell whether to commit the claim separately (at-most-once, may drop) or together (at-least-once, may re-fire).
2. **§2.3 has the same defect, and there it is load-bearing.** "The submit and the update share one transaction; a crash in between leaves the row due and re-fires — and the reason `Idempotency-Key "schedule:{id}:{next_run}"` is used". If it is one transaction, the crash also rolls back the idempotency row, so the key protects nothing. The key only helps if the submit commits separately.
3. **Scheduled jobs can never touch the vault or proxy.** §2.2 sets `effective_grant = intersect(owner.effective_now, system:worker.grant)`, and 04 seeds `system:worker` with `"vault":{"read":[],"write":[],"quarantine":[],"promote":[],"deny":[]}` and `"proxy":[]`. Intersection with empty lists is empty. Meanwhile automations (§6) intersect only with `owner_grant` and are unaffected. Two trigger mechanisms, silently different capability ceilings, and the schedule one is almost certainly not intended.
4. `system:worker` is tier 3, so `intersect` caps every scheduled job's frozen grant at tier 3 regardless of owner. Probably intended, never stated.
5. `config._seen_uids` and the automation's own `next_run` are engine-written keys inside a document whose validator (§3.3) says "unknown keys anywhere are errors". No exemption is specified, and this is a read-modify-write on a jsonb column — the exact shape of defect 04's codec note exists to prevent.
6. Nothing specifies who may create a `webhook`-triggered automation at what tier, nor any rate limit on `/hooks/{path}`, which is an unauthenticated public path (10 §1).

---

## 10 — REST API

**What it specifies.** Every HTTP endpoint under `/rest/v1/` plus root-level service paths, the four-layer middleware order, a stable error envelope, cursor pagination, and per-route tier requirements. Roughly 80 routes across system, catalogue, jobs, connections, principals, vault, schedules, automations, services, audit, service paths and the UI shell.

**Concrete details worth knowing.**
- **Middleware order, outermost first**: Trace (mint uuid4, read `X-Trace-Id` into `client_trace_id`, echo server value) → Auth (fail closed; errors rendered directly because **middleware sits outside exception handlers**, with `WWW-Authenticate` preserved) → Audit → Rate limit. That auth-outside-exception-handlers note is a specific, hard-won detail.
- **Audit policy is selective and stated**: every non-GET, plus GETs on `/stream`, `/download`, `/serve`, `/rest/v1/vault/*`, `/rest/v1/jobs/*/artifacts/*` — "reads that hand out content". Plain listing GETs are not audited.
- Error envelope `{status, code, message, detail, trace_id}` with an enumerated stable code list (28 codes). "Clients branch on `code`, never on `message`."
- Pagination: `?limit=` default 50 max 500, `?cursor=` opaque base64 of the last row's sort key, `total` only when cheap.
- **`/stream`, `/download`, `/upload` refuse cookies on purpose**, with the reason given: "a link on any page could otherwise run a workspace as the signed-in viewer". `SameSite=Lax` would not stop a top-level GET. This is correct and is guarded (AUTH-002).
- `POST /rest/v1/jobs/submit/...` takes `Idempotency-Key` and `X-Priority` (−10..10, tier ≥4 for >0), returns **202 + `Location`** normally and **200 + `Location`** when the key matched.
- **The first principal is created by CLI only** — "there is nobody to authenticate the request that would create it". `/transformations/…` is kept as a **301 alias** for four legacy routes.
- Tier-4 callers on `GET /rest/v1/principals` see only principals whose effective grant ⊆ theirs.

**Contradictions and gaps.**
1. `DELETE /rest/v1/principals/{name}` → 409 if it has children, contradicting 02 §4's "children are disabled" (see 02).
2. **`GET /rest/v1/jobs/{id}` at tier 2 returns the job row**, which per 04 contains `params` **and** `effective_grant`. 14 goes out of its way to keep parameter values out of audit; this route hands them, plus another principal's frozen grant, to any tier-2 principal sharing the repository. No field-level redaction is specified.
3. **No rate limiting exists for unauthenticated endpoints.** §1 puts rate limiting last in the chain and sources it "from the principal's effective limits". `/oauth/register` (creates rows, unauthenticated by RFC 7591), `/hooks/{path}`, `/ui/signin` (password lockout only) and public `/serve/` have nothing.
4. `/serve/{name}` is listed as a public path "when the service is `public`", which requires a database lookup inside the auth middleware — a dynamic public path. Workable but not called out, and it means the auth layer depends on the hosted-services table.
5. There is no route to delete a workspace version (see 02) and no route to list or manage `workers` beyond `/health`.
6. `PATCH /rest/v1/automations/{id}` is documented as `{enabled}` only "no reparse" — but 09 §3.3 freezes `owner_grant` at save, so re-enabling a stale automation resurrects a grant frozen arbitrarily long ago without revalidation. The `intersect(owner.effective_now, …)` at fire time covers narrowing; it does not cover a connection that has since been re-tiered.

---

## 11 — MCP Surface

**What it specifies.** A stateless Streamable-HTTP JSON-RPC 2.0 endpoint at `POST /mcp` (protocol `2025-06-18`) that builds its tool catalogue per request from the caller's effective grant, maps manifest parameters to JSON Schema, and solves long-running work with a job-handle pattern instead of a server stream. Also covers resources, delegation-from-MCP, and trace propagation.

**Concrete details worth knowing.**
- **Deliberately stateless**: no session id issued or required, `Mcp-Session-Id` ignored if sent, `GET /mcp` → 405 with `Allow: POST` because "the server has nothing to push". Batching → `-32600`; notifications → 202 empty body.
- **The job handle is the central idea.** `_wait_seconds` is injected into every workspace tool's schema (0..300, default from `manifest.mcp.wait_default_seconds` or 45). On timeout the call returns a **non-error** result with prose telling the model what to do next plus `structuredContent {job_id, status, pct, resource}`. "This pattern is what lets a workspace with a 20-minute timeout be an MCP tool without holding an HTTP connection open for 20 minutes."
- **A failed workspace is `isError: true`, not a JSON-RPC error** — "the model must see it to react" — carrying the job error and the last 50 log lines.
- Result assembly is concrete: text-ish MIME types inlined and truncated at 64 KiB with a trailer pointing at the resource URI; `image/png|jpeg|gif|webp` ≤1 MiB inlined as base64 (**the one binary case, because vision-capable clients can use it**); everything else a `resource_link` with `datum://jobs/{id}/artifacts/{name}`; `service/*` becomes a text block with the absolute `/serve/` URL. `structuredContent` always carries `{job_id, status, artifacts[], duration_seconds}`.
- **Hidden and nonexistent tools give the same error** (`-32602 "unknown tool"`), guarded as MCP-002.
- Built-in tool table gates visibility on the grant, not just tier: `vault_*` shown only when `vault.read` is non-empty, `proxy_request` only when `connections.proxy` is non-empty, etc. "Fewer tools rather than broken ones."
- `delegate_create({name, grant, expires_in_seconds?})` returns the raw token **once**, default 24 h, capped by `policy.delegation.max_token_hours` — "how a dispatching agent creates a research drone with a sliced vault scope without an operator in the loop".

**Contradictions and gaps.**
1. **§3 contradicts its own stated principle.** Workspace tools are listed for "every workspace whose current version declares `mcp` and whose repository is in the caller's scope" — **no tier filter**. But calling one requires tier 3 (06 §2). A tier-1 or tier-2 caller therefore sees a full catalogue of tools that can only ever return 403, directly against "a tool that could only ever return 403 for this caller is not listed" three paragraphs later. MCP-001 guards repo scope only; MCP-004 guards built-ins only.
2. A tool-name collision raises `-32603` "naming both" — at `tools/list` time this fails the **entire** catalogue for every caller until an operator intervenes. Given names are `{repo}__{ws}` with unsafe chars replaced, a collision is reachable (`A_B/C` vs `A/B_C`). Excluding the pair with a warning would be the graceful choice; the doc picks the loud one without acknowledging the blast radius.
3. `resources/list` returns "the caller's 50 most recent jobs' primary artifacts". Scope re-checking on `resources/read` is specified only for `job_status`/`job_result`; the `datum://` read path's authorisation is left implicit.
4. `_wait_seconds` is injected into the parameter schema but manifest parameter names are `^[A-Z][A-Z0-9_]{0,63}$`, so no collision is possible — that is fine, but the doc never says so, and an implementer might add a defensive check or, worse, not.
5. Nothing specifies behaviour when `tools/call` is made against a workspace whose current version changed between `tools/list` and the call.

---

## 12 — OAuth 2.1 Authorization Server

**What it specifies.** A hand-rolled AS supporting exactly one grant type family (authorization_code + refresh_token), S256-only PKCE, RFC 7591 dynamic registration, RFC 9728/8414 discovery, RFC 8707 resource indicators, and RFC 7009 revocation. Tokens carry no permissions — they resolve to a principal whose grant is enforced.

**Concrete details worth knowing.**
- **Token exchange pseudo-code is given line by line**, including `SELECT ... FOR UPDATE` on the code row and the check order: client match → **`used_at` non-null ⇒ replay ⇒ revoke the whole family** → expiry → exact `redirect_uri` → `b64url(sha256(code_verifier)) == code_challenge` → resource match → client secret.
- **Refresh rotation with reuse detection**: `rotated_to` non-null on presentation means someone else has a copy ⇒ `revoke_family(client_id, principal_id)`.
- **`revoke_family` runs in its own transaction** "so the failure response cannot roll it back — the original documents this exact bug and its fix, and it is registered as a guard". OAUTH-003. This is the single sharpest detail in the corpus: a specific past defect encoded as a transaction-boundary requirement plus a mutation test.
- `redirect_uri` matching is **exact** — the doc explicitly says trailing-slash normalisation is *not* done — and a mismatch renders an error page and **does not redirect**.
- `code_challenge_method` must be `S256`; `plain` refused. `resource` defaults to `{PUBLIC_URL}/mcp` and cannot widen at token time (`invalid_target`).
- **Consent is per authorization; there is no "remember this client"** — the principal sees which client and which audience every time. Consent requires `kind='human'`, not disabled, tier ≥1, with generic failure messages and the §9 lockout.
- `PUBLIC_URL` is required at startup because "a wrong value serves discovery happily and mints tokens no client can use". TTLs: access 3600 s, refresh 30 d, code 10 min.
- Unused client registrations pruned after 30 days.

**Contradictions and gaps.**
1. **`canonical()` is never defined.** It appears in 03 §7 (`canonical(cred.audience) != canonical(MCP_RESOURCE)`) and twice here. Trailing slash? Case folding of scheme/host? Default port elision? Given the doc goes out of its way to say redirect URIs are matched with *no* normalisation, the asymmetry is deliberate-looking but unspecified. Likewise `MCP_RESOURCE` appears in 03 but is not in 16's environment table.
2. **`scope` is decorative.** It is stored on `oauth_codes` and `credentials`, advertised as `scopes_supported: ["mcp"]`, and consulted by nothing in 03 §7 or anywhere else. The intro half-acknowledges this ("the grant carries no permissions of its own") but a client that requests a narrower scope gets no narrowing, which is a spec-visible lie to the client.
3. **`/oauth/register` is unauthenticated with no rate limit and no cap.** The doc notes "a client id grants nothing on its own", which is true, but it is an unbounded public row-insert; pruning is at 30 days.
4. The replay path says `revoke_family(...)` runs "outside the transaction that is about to fail" while holding `SELECT ... FOR UPDATE` on `oauth_codes`. The interleaving of the two transactions (and the lock held across the second) is not spelled out; an implementer could deadlock here.
5. No consent screen for the *audience* being a non-`/mcp` resource, because only one is allowed — fine, but that also means the `resource` parameter is effectively a constant, so RFC 8707 support is nominal.

---

## 13 — Hosted Services

**What it specifies.** Two kinds of long-lived URL under `/serve/{name}/`: `static`, created only by a job producing a `service/*` artifact, serving files directly out of that job's artifact directory; and `proxy`, registered only by an admin, reverse-proxying to an origin. Covers visibility, the serving algorithm, PWA-specific headers, and dashboard CSP.

**Concrete details worth knowing.**
- **Supervised subprocess services are explicitly removed** as a kind: "A workspace that wants a long-lived process runs it elsewhere and an admin fronts it with a `proxy` service. The gateway's process model stays one API and N workers." A clean scope cut with a named replacement.
- **"The artifact is the site."** Nothing is copied to a webroot; re-running swings `path` to the new job's directory; **rollback is `PATCH source_job` back**, surfaced as "revert to previous run"; the retention sweeper never deletes a directory a service points at (SERVE-009).
- **The registration SQL is given verbatim** with `ON CONFLICT (name) DO UPDATE … WHERE hosted_services.kind='static' AND repository=EXCLUDED.repository AND workspace=EXCLUDED.workspace RETURNING name` — **no row returned means the name is held by someone else and the job is finished `failed`**. Name theft is prevented by a WHERE clause rather than a code check, which is the right level.
- Serving: `GET /serve/{name}` → **308** to the trailing slash; static uses `(root/path).resolve()` + `is_relative_to(root)` else 404, directory → `index.html`, **never lists directories**, `Cache-Control: no-cache` + ETag from mtime+size.
- **`/serve/` is the one service-shaped path that accepts the session cookie, "because it executes nothing"** — the exact inverse of 10 §14's rule for `/stream`. The reasoning is stated on both sides and consistent.
- Proxy: `trust_env=False`, `follow_redirects=False`, 30 s connect / 300 s read; forwards everything **except hop-by-hop and `Authorization`/`Cookie`** ("the gateway's credentials must not leak to the origin"); adds `X-Forwarded-*` and `X-DG-Principal`/`X-DG-Tier` so the origin can do coarse authorisation of its own. **Private-range origins are allowed here and only here** — the one documented exemption from the egress rule, admin-only and audited.
- CSP is type-specific: `service/dashboard` gets `default-src 'self'; connect-src 'self'` **so a dashboard cannot exfiltrate a session cookie to a third-party origin**; `static`/`pwa` get `default-src 'self' data: blob:`.
- §7 on PWAs is unusually practical and correct: `*.webmanifest`/`manifest.json` as `application/manifest+json`; `sw.js` with **`Service-Worker-Allowed: /serve/{name}/`**; the publish gate **warns, not fails**, when a PWA manifest lacks `start_url`/`scope` equal to `/serve/{name}/` or 192px+512px icons; and the secure-context reality is stated plainly — `localhost` counts, `http://192.168.x.x` on a phone does **not**, so LAN installs bookmark instead of installing and you must use the HTTPS `PUBLIC_URL`.

**Contradictions and gaps.**
1. **A bad manifest visibility crashes the finish transaction.** §3's INSERT passes `$vis, $tier` straight from the manifest output, and 04's `CHECK ((visibility='tier') = (min_tier IS NOT NULL))` will reject `visibility:"tier"` without `min_tier`. The publish gate (05 §5 check 7) validates `service_name`, type servability and ownership — **not** visibility/min_tier coherence. The failure surfaces at job-finish as a database error, not at publish as a validation message.
2. **WebSocket proxying is one sentence with no auth story.** "WebSocket upgrade is supported via the ASGI `websocket` scope with a bidirectional pump". 10 §1's middleware chain, the visibility check, the cookie-vs-bearer rule, `X-DG-Principal` injection and audit are all specified for HTTP only. No guard covers it. An implementer will either skip it or build an unauthenticated tunnel.
3. §3 says static visibility defaults come from the manifest output and "only an admin can later change it" — but the `ON CONFLICT DO UPDATE` set-list is `type, source_job, path, updated_at`, deliberately **not** visibility. So a manifest changing its declared visibility silently has no effect after first registration. Correct behaviour, undocumented consequence.
4. `insecure_tls: true` "logs a warning per request" — no rate limiting on that log, and no audit flag despite being a security-relevant setting (contrast `allow_private_origin`, which is `governance: true`).
5. Nothing specifies what `/serve/` does when `source_job`'s artifact directory has been removed — the sweeper is told not to, but a manual `DELETE /rest/v1/jobs/{id}` path or an operator `rm` leaves a row pointing at nothing. Presumably 404; unstated.

---

# SPECIFICATION QUALITY ASSESSMENT

**Verdict: this is genuinely rigorous, well above the median for a document set of this size, and it is buildable — but not from these thirteen documents alone without an implementer making around a dozen judgement calls, two of which are security-relevant.**

### Evidence that it is real, not decorative

The reliable tell for a hollow spec is that it names mechanisms without giving them. This one gives them:

- **Verbatim SQL for the hard concurrency paths**: the `FOR UPDATE SKIP LOCKED` claim, both reaper statements, the conditional `finish` (`WHERE … AND lease_worker=$5`), the delivery claim, the schedule due-scan, and the `ON CONFLICT … WHERE` that prevents hosted-service name theft. These are the four places a job engine actually goes wrong, and all four are specified at the statement level.
- **Algorithms, not descriptions**: the content hash is a byte-level formula; `subsumes()` is pseudo-code; sealed-blob layout is byte offsets; the OAuth token exchange is a check-ordered listing; the child's fd remapping (`dup(1)`, `dup2(2,1)`) is exactly right and specified to happen *before* the workspace is imported.
- **Numbers everywhere and mostly consistent**: 60 s lease / heartbeat at lease/6, 5 s kill grace, 30 s drain, 256 KiB vault read, 4 MiB vault write, 1 MiB proxy response, 64 KiB MCP inline, 2000 list entries, 200 search results, 5 delivery attempts with `min(2^n·30s, 1h)`, 10-min code TTL, 5/15min lockout, chain depth 8, delegation depth 4.
- **Structural rather than procedural security.** The secret is excluded from a shared `_COLUMNS` constant so no route can leak it by forgetting; AAD-binds the blob to the connection name so an UPDATE cannot relocate a privileged secret; name ownership is a WHERE clause; the one-live-password rule is a partial unique index; the lease invariant is a table CHECK. This is a designer who has been burned by code-level checks and moved them into places that cannot be omitted.
- **Rationale attached to non-obvious choices**, which is what lets a reviewer catch a wrong one: why 403-not-404 on vault (existence leak), why write-does-not-imply-read at decision time (so deleting the validator fails a test), why `/stream` refuses cookies but `/serve` accepts them (one executes, one does not), why unknown/expired tokens share a status but not a code, why `revoke_family` needs its own transaction, why the reaper replaced a single-worker advisory lock.
- **Doc 17 is the strongest artefact in the set.** ~130 registered guards, each with a file, an exact `remove` string, a replacement, a named test and a doc citation, driven by a harness that requires the test to *fail* when the guard is removed, treats a **skipped** test as UNPROVEN, runs each case in a fresh subprocess so import caches cannot hide a mutation, and refuses to start on a dirty tree. That is the difference between "we have tests" and "we have evidence the tests test something".

### Where it is hand-wavy

- **The workspace/vault trust boundary is asserted, not built.** 08 §9 claims a job cannot exceed its frozen vault slice; the enforcement is a helper the workspace imports voluntarily, in a subprocess with no sandbox by default. VAULT-021 will pass while the property is false. Everything else in the corpus about delegated authority — the whole drone story in 11 §8 — rests on this. It needs either a real sandbox (bind-mount/seccomp) or an honest downgrade of the claim.
- **Version rollback does not work.** Versions are called immutable and hash-addressed; no bytes are stored; `activate` requires the disk to still match. Two documents describe a capability the schema cannot deliver.
- **Scheduled jobs get an empty vault scope and empty proxy list** because `intersect` meets the owner against a seeded `system:worker` grant with empty arrays. This is a one-line consequence of two documents that were plainly written apart, and it silently disables a whole category of intended use.
- **`narrows()`/`intersect()` cover five of ten grant blocks.** Doc 21 introduced `memory`/`code`/`compute`/`documents`/`mcp` and registered guard FED-015 for their narrowing, but never went back and amended 03 — the document that opens by declaring itself load-bearing. That is the classic failure mode of a corpus grown by appending.
- **Two self-contradicting transaction-boundary paragraphs** (09 §2.3 and §5), both in the exact place where at-most-once vs at-least-once is decided. These will be implemented wrong roughly half the time.
- **Unauthenticated surface has no rate limiting at all.** `/oauth/register`, `/hooks/*`, public `/serve/`. Rate limiting is defined purely as a per-principal grant limit.
- **Naming drift with 16**: `DATUM_GATE_AUTH`/`DG_AUTH`, `WORKER_COUNT`/`API_COUNT`/`API_PROCESSES`, `policy.max_delegation_depth`/`policy.delegation.max_depth`. Plus `KILL_GRACE_SECONDS` and `MCP_RESOURCE` are referenced but absent from the environment table, and `canonical()` — which two documents use for an authentication decision — is never defined.
- **Two performance holes nobody costed**: an ancestor walk plus two `UPDATE`s on every authenticated request, and one `audit_log` row per `/serve/` request (13 §5 says "the audit writer batches to keep this cheap", which is a hope, not a design — a static site is dozens of requests per page load).

### Would I build from it?

Yes, with a one-page errata first. The corpus is unusually honest about its own reasoning and unusually specific where specificity is expensive. Its defects are almost all of one kind — **later documents amended the model and earlier documents were not revisited** (02 vs 04 on principal deletion and the name regex; 03 vs 21 on grant blocks; 04 vs 05 on `max_attempts`; 09 vs 04 on `system:worker`; 05 vs 10 on rollback). That is a cheap class of defect to fix and a good sign about the underlying thinking, but it does mean the "every document is self-contained enough to build its component from" claim in 00-README is false in at least five places, and doc 03's self-description as *the* load-bearing authority document is now inaccurate.

The one thing I would not ship as written is 08 §9. A guard that proves the wrong thing is worse than no guard, and this corpus is explicitly built on the premise that it knows the difference.