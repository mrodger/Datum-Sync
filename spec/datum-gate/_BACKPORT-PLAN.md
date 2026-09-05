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

| | Item | Touches | Reversible | Depends on |
|---|---|---|---|---|
| B1 | Guard registry with stable IDs | tests only | yes | — |
| B2 | One `audit_log` + server-minted trace id | +1 table | yes | B1 |
| B5 | Multiple labelled tokens per principal | +1 table | yes | B1 |
| B4 | Multi-key secrets (key-id byte) | `connections.secret` bytes | yes, with both keys | B1 |
| B3 | Single grant document | 4 columns → 1 | needs a down path | B1, B2 |

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

### The change

Adopt doc `17`'s registry format — `guards.yaml`, one entry per guard:

```yaml
- id: SECRET-004
  test: tests/test_connections.py::test_no_read_path_returns_the_secret
  file: datum_sync/connections.py
  remove: "    (secret IS NOT NULL) AS has_secret\n"
  replace: "    (secret IS NOT NULL) AS has_secret, secret\n"
```

and three rules in the runner:

- `remove` must occur **exactly once** in `file`, or the case is an ERROR, not a pass.
- A test that **skips** is reported UNPROVEN, never PASS. This is the clause worth
  the whole exercise; it closes a failure mode this project has hit before.
- Every entry's `id` must be referenced by a `Guard: <ID>` line in the named test,
  and every `Guard:` line in `tests/` must have an entry. Both directions, so
  neither a registry entry nor a test can drift away alone.

`test_schema_drift.py` already carries `Guard: SCHEMA-001`; it is the only one.

### How it is proven

Delete a `Guard:` line from a test → the cross-reference check fails. Edit an
anchored source line so `remove` no longer matches → ERROR, not PASS. Point a case
at a test that skips → UNPROVEN. All three must be demonstrated before the registry
is trusted, because the whole claim of this item is that the harness now notices
things it used to swallow.

### Cost and risk

Test-only. No runtime change. The 1557-line CASES list has to be transcribed, which
is the bulk of the work and is mechanical.

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
