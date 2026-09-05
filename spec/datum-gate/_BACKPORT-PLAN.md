# Backport plan — Datum-Gate ideas into Datum-Sync

**Date:** 2026-09-05
**Decision:** merge into this repository. No `datumgate` rewrite.
**Basis:** `_README-review.md` (verdict), `_REVIEW-decision-log-audit.md` (what the
fable gets wrong about us), `_REVIEW-docs-02-13.md`, `_REVIEW-docs-14-23.md`.

Every "what exists now" line below was read out of the source or the live database
on :5435 on the date above, not taken from the fable's account of us. The fable is
wrong about our codebase in three places, so its gap analysis cannot be used
directly as a work list.

---

## Sequencing

**B1 goes first and alone.** It is the only item that changes how the other four
are verified. Doing it after the schema work means the schema work is proven by a
harness we already know has a silent-disarm failure mode.

**B2 and B5 are additive** — a new table, and a new table plus a read path. Neither
invalidates an existing row.

**B3 and B4 rewrite data that is already in production.** They come last, and each
needs a reversible migration, because a wrong `intersect()` or a wrong key byte
locks the tier-4 credentials out of the only process that can read them.

| | Item | Touches | Reversible | Depends on | Status |
|---|---|---|---|---|---|
| B1 | Guard registry with stable IDs | tests only | yes | — | **done** |
| B2 | One `audit_log` + server-minted trace id | +1 table | yes | B1 | **done** |
| B5 | Multiple labelled tokens per principal | +1 table | yes | B1 | |
| B4 | Multi-key secrets (key-id byte) | `connections.secret` bytes | yes, with both keys | B1 | |
| B3 | Single grant document | 4 columns → 1 | needs a down path | B1, B2 | |

---

## B1 — Guard registry with stable IDs

### What exists now

`tests/break_the_guard.py`, 1557 lines. A Python `CASES` list of five-tuples:
`(label, file, old, new, test)`. It deletes each guard from the source, runs the
named test, and requires it to fail.

The mechanism is right. Three properties it lacks, all of which have already cost
us something:

1. **No stable ID.** A guard is identified by a prose label and by the exact source
   string it removes. Ordinary feature work that edits an anchored line silently
   disarms the case — the harness reports a pass because the `old` string is no
   longer found, and a case that cannot apply is indistinguishable from a case that
   applied and worked.
2. **A `new` string matching more than one call site breaks several guards at once**
   and therefore proves none of them: the named test fails, but not because of the
   guard under examination.
3. **A skipped test counts as a pass.** `test_schema_drift.py` skips when the
   database is unavailable. Under the current harness that skip is silently
   equivalent to "the guard held".

> **Correction, 2026-09-05 — points 1 and 3 above are wrong, and were wrong when
> written.** They were reasoned from the corpus's account of a harness like ours
> rather than from `break_the_guard.py`, which is the same mistake this document
> warns about two paragraphs from the top. Read before implementing:
>
> - **Point 1 is false.** `break_the_guard.py:1519-1524` already refuses a case
>   whose anchor does not match exactly once, prints `SKIP`, and `:1550` returns 1.
>   It is loud. An audit of all 130 cases found **0 dead anchors** — but **1
>   ambiguous** one (`SERVICE-013`, whose anchor came to match a second, identical
>   filter in the published-workspace listing), which had the harness exiting 1.
> - **Point 3 is false, and backwards.** A skipped test exits 0, which the old
>   `run()` read as `passed=True`, which `main()` reported as **UNPROVEN**. Skips
>   already landed on the conservative side.
> - **The real hole, not mentioned above.** `run()` collapsed every non-zero exit
>   into "the guard broke its test". pytest exits **4** for a test id that does not
>   exist and **2** for a collection error, so a case whose test was renamed or
>   deleted printed `ok` — a guard counted as proven by a run in which nothing
>   executed. Measured, not assumed. All 121 distinct test ids collect today, so
>   this was latent rather than actively lying.
>
> Net effect on the work: the ID/cross-reference half stands unchanged; "skip ⇒
> UNPROVEN" and "anchor must match once ⇒ error" are existing behaviour to be
> preserved, not built; and outcome classification — which this document did not
> ask for — is the item that actually closes a silent pass.

### The change

**As built (2026-09-05), the registry is Python, not YAML.** The 130 anchors are
whitespace-exact multi-line strings; re-encoding them as YAML block scalars is 130
chances to alter a string and buys no functional difference, since the runner
reads them back into the same `str`. The id was inserted as a sixth tuple field
using AST source positions, so no anchor byte was rewritten — verified by parsing
the pre- and post-edit files and diffing the case data, which found exactly one
changed case: `SERVICE-013`, the ambiguous anchor, changed on purpose.

The format doc `17` specifies, retained for the record:

```yaml
- id: SECRET-004
  test: tests/test_connections.py::test_no_read_path_returns_the_secret
  file: datum_sync/connections.py
  remove: "    (secret IS NOT NULL) AS has_secret\n"
  replace: "    (secret IS NOT NULL) AS has_secret, secret\n"
```

and the rules in the runner:

- `remove` must occur **exactly once** in `file`, or the case is an ERROR, not a
  pass. *(Existed; the SKIP was promoted to ERROR wording and `SERVICE-013` fixed.)*
- A test that **skips** is reported UNPROVEN, never PASS. *(Existed.)*
- **The named test must actually run.** Its outcome is read from a junit report
  rather than inferred from the exit code, so NOTFOUND and collection ERROR are
  errors instead of proofs. *(New — this is the one that closed a silent pass.)*
- Every entry's `id` must be referenced by a `Guard: <ID>` line in the named test,
  and every `Guard:` line in `tests/` must have an entry. Both directions, so
  neither a registry entry nor a test can drift away alone. *(New.)*
- A guard that **cannot** be proven by a source edit is registered in `UNPROVABLE`
  with a stated reason, printed on every run, and must still be cited by a test.
  Otherwise "no break case" becomes the next silent pass. *(New — `SCHEMA-001`,
  whose guard is broken by altering a database, not a file.)*

The cross-reference checks live in `tests/test_guard_registry.py` so they run in the
ordinary suite, not only in the half-hour harness — including an AST existence check
for every named test, which catches a rename in milliseconds rather than in 30
minutes.

### How it was proven

Seven mutations, each reverted, each confirmed to fail the intended check:
delete a `Guard:` line; rename a named test; cite an unregistered id; remove the
only citation of `SCHEMA-001`; break an anchor (→ ERROR); point a case at a
skipping test (→ UNPROVEN); point a case at a missing test (→ ERROR/NOTFOUND,
where the old runner printed `ok`).

### Cost and risk

Test-only. No runtime change. No transcription: the ids were inserted in place by
AST position, so the 1557-line list never had to be retyped.

---

## B2 — One `audit_log`, and a trace id the server mints

### What exists now

Four tables, none of which can be joined to another:

| Table | Columns |
|---|---|
| `job_log` | `id, job_id, ts, level, message` |
| `proxy_log` | `id, agent_id, agent_name, account_name, connection_name, method, path, upstream_status, created_at` |
| `mcp_call_log` | `id, account_id, account_name, method, tool_name, target, is_governance, outcome, error_code, duration_ms, client_trace_id, created_at` |
| `automation_runs` | `id, automation_id, fired_at, trigger_job, results, ok` |

`mcp_call_log.client_trace_id` is read at `datum_sync/mcp.py:546` from the
`X-Trace-Id` **request header**. It is supplied by the caller, so it is neither
unique nor trustworthy: an agent can send the same value on every call, or reuse
another agent's. `proxy_log` has no trace column at all.

**The concrete consequence:** an MCP tool call that issues a proxy request writes
two rows in two tables with nothing in common but a timestamp. "Which upstream did
this tool call touch" is not answerable today.

### The change

Per D14/D15: one `audit_log` with a discriminator column, and a trace id **minted by
the server** at request entry, carried through the call, and written on every row
the request produces. Keep the client value in a separate `client_trace_id` column
— useful, but never the join key.

Do not drop the four tables in the same migration that adds the new one. Dual-write
first, verify the new table answers the questions the old ones do, then retire them.

### How it is proven

Issue one MCP tool call that provokes a proxy request; assert exactly one trace id
covers both rows. Then send a request with a forged `X-Trace-Id` equal to another
request's server trace and assert the two are still distinguishable — the point of
minting is that a client cannot merge or split someone else's trace.

### What was built — DONE

`migrations/011_audit_log.sql`, `datum_sync/audit.py`, wired into `mcp.py`, `proxy.py`
and `api.py`. 18 tests in `tests/test_audit_trace.py`; 9 new guards (`AUDIT-001..009`,
`PROXY-006`), each break-tested. Suite 565 → 584.

The plan's claims above were checked against source before any code was written, since
B1's were not all correct. This time they held: `grep` confirms `proxy_request` has
exactly one production caller (`mcp.py`), so every `proxy_log` row does have a sibling
`mcp_call_log` row and nothing joins them.

Decisions worth recording, because each closed off a cheaper option:

- **The trace is threaded explicitly, with no default**, rather than carried in a
  `ContextVar`. A `ContextVar` that is not set yields a row that inserts cleanly and
  joins to nothing — an invisible failure in the one table whose job is to be
  complete. A required parameter turns the same mistake into a `TypeError` at the call
  site. It did: wiring it broke three call sites loudly and immediately. Same reasoning
  as `Principal.vault_scope`, which documents it.
- **`Trace` is a frozen dataclass**, not two adjacent `str` parameters. The trusted id
  and the caller-supplied string threaded side by side through six functions are
  trivially swappable, and a swap would promote the forgeable value to the join key
  while every "the rows share a trace" test kept passing.
- **The trace is not on `Principal`.** That object answers "who is calling and what may
  they reach"; a request id is not identity.
- **`outcome` permits only `'ok'` and `'error'`, not `'denied'`.** Nothing in the
  codebase can currently produce a distinguishable denial — an access check raises
  `ApiError`, which becomes an `isError` result inside a *successful* JSON-RPC
  response, recorded as `ok`. Permitting a value nothing writes advertises a
  distinction the data does not carry. Fixing the classification is a separate change.
- **Phase 1 reproduces the old classification deliberately**, warts included, so that
  `audit_log` and `mcp_call_log` must agree row for row. That agreement is the check on
  the new table before anything is retired; improving the classification here would
  give any disagreement two possible causes instead of one.
- **`audit_dropped` is reported on `/health`.** `audit.write` swallows its exceptions
  like every writer beside it, which is right for a log and wrong for an audit trail.
- **Not built:** the spec's bounded queue, background drain, retention sweeper and
  export CLI. A queue changes failure and ordering behaviour, and doing that in the
  same change that adds the table would mean two candidate causes for any surprise.

Two things worth carrying forward, both found by a check rather than by reading:

- **The `/health` guard was initially unfalsifiable.** `audit_dropped == audit.dropped()`
  on a clean process compares zero to zero and passes just as happily against a
  hardcoded `0`. Confirmed by measurement, not assumed: the weak version reports
  `PASSED` (i.e. UNPROVEN) under the break. It now forces a drop first.
- **B2's own edits silently disarmed `MCPLOG-003` and `MCPLOG-004`** — both anchored on
  a `client_trace_id` local that the `Trace` refactor deleted, so their anchors matched
  zero times and their breaks would have removed nothing. The full suite passed at 584
  without noticing. This is the failure mode B1 predicted and did not check for, so
  `test_guard_registry.py` gained `test_every_case_anchor_still_matches_exactly_once`:
  a string count, milliseconds, and the only thing standing between ordinary feature
  work and a guard that has quietly stopped guarding. Both cases were re-anchored and
  re-proven.

`AUDIT-007` (no audit row carries the proxy's injected secret) is registered
`UNPROVABLE`: it holds by construction, so the change that would falsify it is an
addition, and the harness proves a guard by deleting one.

---

## B5 — Multiple labelled tokens per principal

### What exists now

`service_accounts.token_hash` is a single `text` column with a **UNIQUE constraint**,
plus `token_expires`. One principal, one token.

Rotation is therefore: overwrite the column. Every holder of the old token is
broken at the instant of the write, and there is no window in which both work. For
a fleet with more than one process per account there is no safe rotation at all —
this is a real operational gap now, not a hypothetical.

### The change

A child table: `(id, account_id, label, token_hash UNIQUE, expires_at, created_at,
last_used_at, revoked_at)`. Auth resolves a presented token against the child table.
`label` so an operator can revoke "the CI runner" without knowing which hash it is.

Migrate the existing column into one row per account labelled `legacy`, and keep
reading the old column until the new path is proven, then drop it in a later
migration.

### How it is proven

Two live tokens on one account, both accepted; revoke one, assert the other still
works and the revoked one 401s. That second half is the assertion — a test that only
checks the new token works passes just as well against a design that silently
invalidated the old one.

---

## B4 — Multi-key secrets with a key-id byte

### What exists now

`datum_sync/crypto.py`. AES-256-GCM; the sealed value is `nonce || ciphertext || tag`.
The key comes from one environment variable, `DATUM_SYNC_SECRET_KEY`
(`crypto.py:38`, `_load_key` at `:61`). AAD is the connection name (`_aad`, `:101`),
which is what stops an UPDATE moving a tier-4 password onto a connection anyone can
resolve — keep that.

There is no key identifier in the blob. Rotation requires every sealed value to be
re-encrypted in a single step with both keys loaded, which one env var cannot
express. In practice the key cannot be rotated.

### The change

Prefix a version/key-id byte: `keyid || nonce || ciphertext || tag`. Accept a set of
keys (`DATUM_SYNC_SECRET_KEY_<id>`), seal with the current one, open with whichever
the byte names.

Existing blobs have no prefix. Treat a blob whose length matches the old layout as
key-id 0 — but decide this by an explicit stored marker if one can be added cheaply,
because inferring format from length is the kind of rule that is correct until a key
length changes.

### How it is proven

Seal under key A, add key B as current, assert the old blob still opens and new ones
seal under B. Then remove key A and assert the old blob fails **closed** with a clear
error rather than returning garbage — an AEAD failure is loud by construction, but
the error path has to be shown, not assumed.

### Risk

Highest of the five. A mistake here is unreadable production credentials. Take a
verified dump of `connections` before the migration runs.

---

## B3 — Single grant document

### What exists now

Authority is spread across five columns of `service_accounts`: `max_tier` (int),
`repo_scope` (`text[]`), `connection_grants` (`text[]`), `vault_scope` (`jsonb`),
`is_admin` (bool).

The NULL asymmetry D2 complains about is real, and is deliberate and documented:

> `auth.py:124-128` — "NOTE the asymmetry with repo_scope directly above: None there
> means EVERY repository, None here means NO vault access. **They are opposite on purpose.**"

Enforced at `auth.py:143-147` and `vault.py:115-121`, restated in
`migrations/001_core.sql:42` and `007_vault_scope.sql`. It is defensible. It is also
a footgun that survives only because it is written down in four places.

### The change

One `grant` jsonb document per principal with explicit presence semantics — a block
that is absent means no access, for every resource kind, with no exceptions — plus
`narrows()` and `intersect()` helpers.

### Two things to fix in the fable's version before adopting it

1. **`narrows()`/`intersect()` in doc `03` cover five of ten grant blocks.** The
   remaining five are unhandled, and an unhandled block in `intersect()` is what
   produces the empty-scope bug in the fable's own scheduled jobs (`_README-review.md`
   defect 4). Any adoption has to enumerate every block and fail loudly on an unknown
   one.
2. **The argument-guard design cannot express its own flagship example** — a single
   dotted `argument_path` cannot join `owner` + `repo` into `owner/repo`. We do not
   need federation today, so this can be deferred; do not port the guard format as-is.

### What NOT to port

`jobs.delegated_vault_scope` (`migrations/007_vault_scope.sql`) exists but **no Python
file references it** — the fable's D3 describes a substitution rule that was never
built. Freezing a grant onto a job is a **new feature**, not a fix to an existing one,
and it should be argued for on its own merits rather than inherited as a rework.

### How it is proven

The existing auth tests must pass unchanged against the new representation — that is
the whole safety argument for this item. Then, specifically: a principal with **no**
vault block and a principal with **no** repo block must both be denied, which is the
asymmetry being removed. Today one of those two is an allow.

---

## Not adopting

- **Federation (docs 21–23) and the memory layer.** Per the review, these are a
  proposal with a schema sketch, not a spec: version rollback stores no bytes,
  `resources/read` federates with no guards at all, and `16`'s `policy.yaml` is
  missing three blocks that 21–23 require — an invalid policy file is a documented
  startup refusal, so the corpus as shipped would not boot.
- **The workspace/vault trust boundary as written** (`08 §9`). Enforcement is a helper
  the workspace imports voluntarily, in a subprocess with no sandbox. Guard VAULT-021
  passes while the property is false. We have the same weakness; importing their
  guard ID would let us record it as proven.
- **A `datumgate` rewrite.** The four items above are all backportable without it,
  which is the finding that decided this.
