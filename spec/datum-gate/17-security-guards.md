# 17 — Security Guards: Registry and Mutation Harness

A guard is a line of code that refuses something. A test that asserts a
refusal passes just as well against a route broken for an unrelated reason,
so every guard is **registered** with a mutation that removes it and the
test that must then fail. `tests/break_the_guard.py` applies each mutation,
runs the named test, requires failure, and reverts. A guard whose test still
passes with the guard removed is reported **UNPROVEN**, and the suite fails.

## 1. Registry format — `tests/guards.yaml`

```yaml
- id: AUTH-001
  title: bearer token wins over cookie
  file: datumgate/authority/resolve.py
  remove: |
      if raw is not None:
          return await _resolve_bearer(conn, raw, request)
  replace: |
      pass
  test: tests/test_auth.py::test_bearer_beats_cookie
  doc: 03-authority.md §7
```

`remove` must appear exactly once in `file`. The harness verifies the file
is byte-identical after revert and refuses to start if the working tree is
dirty for any registered file. `id` is stable; documents reference guards
by id.

## 2. The registry (initial set)

Every entry below MUST exist in `guards.yaml` with a mutation and a test
before the build step that introduces it is called done.

### Authority (AUTH)

| id | Guard |
|---|---|
| AUTH-001 | bearer wins over cookie |
| AUTH-002 | cookie refused on `/stream`, `/download`, `/upload`, `/mcp` |
| AUTH-003 | revoked credential → 401 |
| AUTH-004 | expired credential → 401 |
| AUTH-005 | disabled principal → 401 on every credential kind |
| AUTH-006 | disabled ancestor → 401 for descendant |
| AUTH-007 | OAuth access token with wrong audience → 401 |
| AUTH-008 | `DG_AUTH=off` refuses non-loopback peer |
| AUTH-009 | `DG_AUTH=off` refuses to start when HOST is not loopback |
| AUTH-010 | password lockout keyed on name, before DB read |
| AUTH-011 | unknown name still pays an argon2 hash (timing) |
| AUTH-012 | one live password per principal |
| AUTH-013 | `calls_per_minute` → 429 |
| AUTH-014 | `WWW-Authenticate` present on 401 |

### Grants (GRANT)

| id | Guard |
|---|---|
| GRANT-001 | child grant must narrow parent (tier) |
| GRANT-002 | child grant must narrow parent (repositories) |
| GRANT-003 | child grant must narrow parent (vault read/write/quarantine by subsumption) |
| GRANT-004 | child promote edges subsumed by parent edges |
| GRANT-005 | child deny ⊇ parent deny |
| GRANT-006 | child limits ≤ parent limits |
| GRANT-007 | tier-3 creator may only create tier ≤2 |
| GRANT-008 | tier-4 may not create tier 5 |
| GRANT-009 | editing a grant that would orphan a descendant is refused and names it |
| GRANT-010 | effective grant is the intersection with ancestors (narrowing a parent narrows the child immediately) |
| GRANT-011 | `admin` must equal `tier >= 4` |
| GRANT-012 | `connections.proxy` may not contain `*` |
| GRANT-013 | write-implies-read coherence |
| GRANT-014 | dead-allow deny coherence |
| GRANT-015 | tier-1 cannot hold write/quarantine/promote/proxy |
| GRANT-016 | delegation depth bound |

### Jobs (JOB)

| id | Guard |
|---|---|
| JOB-001 | submit requires tier 3 |
| JOB-002 | submit requires repo in scope |
| JOB-003 | unknown parameter refused |
| JOB-004 | missing required parameter refused |
| JOB-005 | `FILE` param must be an upload id (never a path) |
| JOB-006 | upload owned by another principal refused |
| JOB-007 | `jobs_per_hour` and `concurrent_jobs` enforced by counting |
| JOB-008 | job runs the version it was submitted against |
| JOB-009 | withdrawn version fails at claim |
| JOB-010 | finish after reaping writes nothing |
| JOB-011 | two workers never both run one job |
| JOB-012 | reaper requeues an expired lease |
| JOB-013 | reaper fails a job out of attempts |
| JOB-014 | child env has no DATABASE_URL and no secret keys |
| JOB-015 | spec goes to stdin, not argv |
| JOB-016 | child stdout is redirected before workspace import |
| JOB-017 | undeclared artifact fails the job |
| JOB-018 | directory under a non-service output fails the job |
| JOB-019 | missing primary output fails the job |
| JOB-020 | artifact name with `/` or leading `.` refused |
| JOB-021 | artifact download path containment |
| JOB-022 | timeout SIGTERM→SIGKILL kills the whole process group |
| JOB-023 | cancel of a job outside scope refused |
| JOB-024 | resubmit uses the caller's grant, not the original's frozen one |
| JOB-025 | frozen grant on a job survives the submitter being disabled |
| JOB-026 | job resolves connections against the frozen grant |

### Publish (PUB)

| id | Guard |
|---|---|
| PUB-001 | `main.py` is parsed, never imported, by the gate |
| PUB-002 | manifest name must match directory |
| PUB-003 | required + default refused |
| PUB-004 | empty MANIFEST.md section refused (`N/A` counts as empty) |
| PUB-005 | undeclared connection refused |
| PUB-006 | connection out of scope refused |
| PUB-007 | write on read-only connection refused |
| PUB-008 | connection tier above publisher tier refused |
| PUB-009 | connection not in publisher `connections.use` refused |
| PUB-010 | service name owned by another workspace refused |
| PUB-011 | `mcp` with required FILE refused |
| PUB-012 | failed gate leaves previous version current |
| PUB-013 | unchanged hash writes no version |
| PUB-014 | smoke test timeout kills the process |
| PUB-015 | unknown manifest keys refused (except `x-*`) |
| PUB-016 | `_venv` internal key refused in user manifests |

### Connections and proxy (CONN)

| id | Guard |
|---|---|
| CONN-001 | `secret` never in any read path (`_COLUMNS` test + response test) |
| CONN-002 | secret-shaped key in `config` refused |
| CONN-003 | AAD binds a blob to its name (swap fails to open) |
| CONN-004 | wrong key / tampered → one indistinguishable error |
| CONN-005 | no key → 503 on writes with secrets, startup succeeds |
| CONN-006 | key id byte selects the opening key; unknown id fails |
| CONN-007 | proxy requires `connections.proxy` membership |
| CONN-008 | proxy requires tier ≥ connection tier |
| CONN-009 | proxy SSRF: private address refused |
| CONN-010 | proxy SSRF: DNS with one private answer refused |
| CONN-011 | proxy re-checks every redirect hop |
| CONN-012 | proxy strips caller `Authorization`/`Cookie` before inject |
| CONN-013 | proxy strips injected auth on cross-origin redirect |
| CONN-014 | proxy bodies never in audit |
| CONN-015 | `allow_private_origin` only settable by tier 5 |
| CONN-016 | delete refused while an active version declares it |
| CONN-017 | per-connection proxy rate limit |

### Vault (VAULT)

| id | Guard |
|---|---|
| VAULT-001 | `..` segment refused, not resolved |
| VAULT-002 | absolute path refused |
| VAULT-003 | backslash refused |
| VAULT-004 | symlink escaping root treated as missing |
| VAULT-005 | deny wins over allow |
| VAULT-006 | `*` does not cross `/` |
| VAULT-007 | `**` matches zero segments |
| VAULT-008 | write does not imply read at decision time |
| VAULT-009 | out-of-scope path → 403 not 404 |
| VAULT-010 | list of uncoverable directory → 403 not empty |
| VAULT-011 | list filters entries individually |
| VAULT-012 | governance write requires policy tier regardless of grant |
| VAULT-013 | governance operations audited with flag |
| VAULT-014 | quarantine write is create-only |
| VAULT-015 | promote requires an edge matching both ends |
| VAULT-016 | promotion approval requires a distinct principal below self-approve tier |
| VAULT-017 | approver must also hold the edge |
| VAULT-018 | source hash change rejects the promotion |
| VAULT-019 | per-path advisory lock on write/promote |
| VAULT-020 | external search results re-filtered by caller scope |
| VAULT-021 | job vault access enforced against the frozen grant |
| VAULT-022 | write size cap; read truncation flagged |

### Triggers (TRIG)

| id | Guard |
|---|---|
| TRIG-001 | `yaml.safe_load` only |
| TRIG-002 | unknown keys refused |
| TRIG-003 | self-trigger refused inside `parse()` |
| TRIG-004 | template outside namespace refused at save |
| TRIG-005 | template render is lookup-only (no attribute traversal) |
| TRIG-006 | trigger with no repository scopes to owner, not all |
| TRIG-007 | loop: job caused by automation does not re-fire it |
| TRIG-008 | loop: chain depth walk refuses cycles A→B→C→A |
| TRIG-009 | jobs finished before automation creation never delivered |
| TRIG-010 | `automations_at` claimed before deliveries (at-most-once consideration) |
| TRIG-011 | delivery dedupe key prevents double insert |
| TRIG-012 | `run_workspace` delivery uses idempotency key |
| TRIG-013 | dead delivery does not block later actions |
| TRIG-014 | egress: private address refused |
| TRIG-015 | egress: redirect re-checked |
| TRIG-016 | egress: response body capped |
| TRIG-017 | automation actions run under `intersect(owner_now, owner_grant)` |
| TRIG-018 | disabled owner stops the automation |
| TRIG-019 | webhook signature verified over raw body |
| TRIG-020 | webhook nonce replay refused |
| TRIG-021 | schedule missed fires once |
| TRIG-022 | schedule re-enable re-times from now |
| TRIG-023 | schedule params validated at write |
| TRIG-024 | schedule fire is idempotent per due time |
| TRIG-025 | `vault_write` action checked against owner vault scope at fire |

### OAuth (OAUTH)

| id | Guard |
|---|---|
| OAUTH-001 | replayed code revokes family |
| OAUTH-002 | replayed refresh revokes family |
| OAUTH-003 | family revocation outside the failing transaction |
| OAUTH-004 | `plain` refused |
| OAUTH-005 | redirect URI exact match only; mismatch does not redirect |
| OAUTH-006 | `resource` cannot widen at token time |
| OAUTH-007 | CSRF on consent |
| OAUTH-008 | consent requires human kind |
| OAUTH-009 | generic failure message on consent |
| OAUTH-010 | refresh with wrong client_id refused |

### MCP (MCP)

| id | Guard |
|---|---|
| MCP-001 | catalogue filtered by repo scope |
| MCP-002 | hidden and nonexistent tools give the same error |
| MCP-003 | required-FILE workspaces never listed |
| MCP-004 | built-in tools hidden when grant makes them unusable |
| MCP-005 | workspace failure is `isError`, not JSON-RPC error |
| MCP-006 | `job_result` refuses out-of-scope jobs |
| MCP-007 | batching refused |
| MCP-008 | every request audited |
| MCP-009 | `delegate_create` enforces narrowing |
| MCP-010 | inline result truncated with resource pointer |
| MCP-011 | 401 carries resource metadata header |

### Serve (SERVE)

| id | Guard |
|---|---|
| SERVE-001 | root containment (resolved, symlink-safe) |
| SERVE-002 | static name owned by another workspace cannot be taken (WHERE clause) |
| SERVE-003 | `principal` visibility requires auth and repo scope |
| SERVE-004 | `tier` visibility refuses lower tier with 403 |
| SERVE-005 | proxy strips `Authorization`/`Cookie` |
| SERVE-006 | proxy adds `X-DG-Principal` only when authenticated |
| SERVE-007 | dashboard CSP injected |
| SERVE-008 | proxy registration is admin-only |
| SERVE-009 | sweeper never deletes a served directory |

### UI (UI)

| id | Guard |
|---|---|
| UI-001 | no `innerHTML`/`outerHTML`/`insertAdjacentHTML`/`document.write`/`eval` in static |
| UI-002 | no external origins in static |
| UI-003 | markdown renderer never passes HTML through |
| UI-004 | shell CSP present |

### Audit (AUDIT)

| id | Guard |
|---|---|
| AUDIT-001 | every non-GET REST request writes a row |
| AUDIT-002 | denied outcomes on governance targets are written synchronously |
| AUDIT-003 | queue overflow increments `audit_dropped`, never blocks |
| AUDIT-004 | `detail` never contains parameter values or bodies |
| AUDIT-005 | visibility of audit rows by tier |
| AUDIT-006 | governance rows survive retention |

## 3. Harness rules

- The harness runs each case in a fresh subprocess so import caches cannot
  hide a mutation.
- A case whose `remove` text is not found exactly once is an error, not a
  skip.
- A test that is *skipped* counts as **not failing**, therefore UNPROVEN.
  The harness prints skip reasons so a poisoned queue or missing fixture is
  visible rather than looking like a clean run.
- The registry is checked in CI: every `id` in this document exists in
  `guards.yaml`, and every entry in `guards.yaml` has a test file that
  exists.
- New guards are added to the registry in the same commit as the code.

## 4. Test environment

- `pytest -q` runs against a throwaway database named by `TEST_DATABASE_URL`;
  `conftest.py` refuses to run if `TEST_DATABASE_URL == DATABASE_URL`.
- A test worker is started in-process by the `worker` fixture for engine
  tests; no test depends on an external worker being up or down.
- `browser_smoke.py` and `break_the_guard.py` are separate gates. All four
  (`pytest`, guards, smoke, plus `ruff`/`mypy --strict` on the package) must
  pass before a build step is called done.
