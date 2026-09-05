# Decision-log audit — Datum-Gate's claims about Datum-Sync

**Date:** 2026-09-05
**Method:** every row of `20-decision-log.md` "Original" column checked against
`/home/marcus/projects/datum-sync/datum_sync/` source, `migrations/`, and the live
DB on `:5435`. Read-only. A claim counts as verified only where code was quoted.

**Why this exists:** the decision log is the fable author's own account of how
Datum-Gate differs from Datum-Sync. It is a *claim about our codebase*, not an
observation of it, and the author appears to have worked substantially from
`README.md` and `PROPOSAL.md` rather than the source. Three rows are flatly false.
Reading the decision log as a gap analysis, without this audit, produces a wrong
picture of what Datum-Sync actually lacks.

## Result

| Verdict | Count |
|---|---|
| TRUE | 27 |
| PARTLY-TRUE | 6 |
| FALSE | 3 |

## The three false claims

### D3 — "`jobs.delegated_vault_scope` substituted for the account scope"

The column exists (`migrations/007_vault_scope.sql`). **No Python file references
it.** `grep -rn delegated_vault_scope datum_sync/*.py` → zero hits. Nothing writes
it, nothing reads it, no substitution happens; a job runs with the submitting
principal's live scope.

*Consequence:* Datum-Gate's frozen-grant-on-job is a **new capability**, not a
reworking of an existing rule. The decision's stated rationale ("what the
original's substitution rule wanted") attributes intent to a rule never built.

### D19 — "Vault graph tools use a cross-database FDW into the orchestrator DB"

False twice over. (1) Datum-Sync has **no vault graph tools at all** — `mcp.py`
exposes workspace tools, `proxy_request`, and `vault_read`/`vault_write`/`vault_list`.
(2) FDW appears only in `PROPOSAL.md:343-352` as a *recommendation for future work*;
`grep -riE "fdw|foreign data wrapper|dblink" datum_sync/ migrations/` → nothing.

*Consequence:* the "Why" column claims Datum-Gate keeps the gateway independent of
another database. It already is independent — the coupling was never built. This row
describes an unstarted proposal as shipped code and inflates the value of the change.

### D36 — "Argument-level control: tool-level only (tool listed or not)"

True only for **workspace** tools. Two of three tool families already enforce on
arguments:

- `proxy.py:157-159` — `if conn_name not in (principal.proxy_grants or [])` — the
  `connection` **argument** checked against a grant list, plus a tier check at `:163`.
- `mcp.py:443-455` — `path` extracted from arguments → `vault.permits(scope, action, path)`,
  glob matching with deny-wins. **The vault scope is an argument guard.**

*Consequence:* the real gap is much narrower than stated — there is no argument guard
for **federated upstream** MCP tools, because (per D31, correctly) there is no
federation at all. Datum-Gate is extending a mechanism we have, not inventing one.

## Materially misleading

**D17 — "vault promote as a glob list, single-actor."** `promote` is a valid scope key
(`vault.py:31`) but **there is no promote operation anywhere**. `vault_fs.py` implements
read, write, list_dir only. `promote`/`quarantine` appear in exactly two places, both
declarations. "Single-actor" implies a working flow a second principal would improve;
there is no flow. Same for quarantine.

## Corrections to the smaller rows

| ID | Correction |
|---|---|
| D21 | Repository scope *is* checked on `/serve/` (`api.py:1677 auth.require_repo`). Missing piece is a per-service visibility column, not all policy. |
| D22 | `rate_limit_per_min` is genuinely dead (one hit, `001_core.sql:54`, no `.py`). But lockout serves both the consent screen (`oauth.py:422`) **and** admin UI login (`ui.py:85`) — "consent-screen only" is wrong. |
| D27 | Accounts are CLI-only, but **agents are already creatable over HTTP** (`api.py:926`). The "delegation needs an HTTP path" rationale is half-satisfied already. |
| D35 | One SQL rollup exists (`api.py:527 /jobs/summary`), so "raw log tables" is not strictly accurate. |
| D23 | Datum-Gate's column says "301 aliases for the old four" — there are **nine** `/rest/v1/transformations/*` endpoints, not four. |

## Confirmed true and worth acting on

D2's NULL asymmetry is real and **deliberately documented as such**:

> `auth.py:124-128` — "NOTE the asymmetry with repo_scope directly above: None there
> means EVERY repository, None here means NO vault access. **They are opposite on purpose.**"

Enforced at `auth.py:143-147` (`repo_scope is None` → True) and `vault.py:115-121`
(`if not scope: return False`). Restated in `migrations/001_core.sql:42` and
`007_vault_scope.sql`. It is defensible, but it is a footgun that a single grant
document would remove.

---

## Finding not in the fable: our migrations no longer describe our database

**FIXED 2026-09-05 — see the closing note at the foot of this section.**

Surfaced while checking D4, and **more urgent than anything in the redesign**:

- Checked-in migrations say four tiers — `001_core.sql:51`
  `CHECK (max_tier BETWEEN 1 AND 4)`, `005_connections.sql:30` same for `tier`.
- **The live DB says five** — `CHECK (max_tier >= 1 AND max_tier <= 5)`,
  `CHECK (tier >= 1 AND tier <= 5)`.
- **No migration file makes that change.** `grep "BETWEEN 1 AND\|<= 5" migrations/*.sql`
  finds only the two `AND 4` lines. The constraint was altered out of band — almost
  certainly by hand to admit the tier-5 superuser in `README.md:11`.
- `schema_migrations` shows **001–007 applied**, while `008_agents_and_proxy_log.sql`
  and `009_mcp_call_log.sql` sit unapplied on disk **despite their tables existing live**.

Application code already assumes 1–5 (`api.py:829`, `connections.py:97`,
`accounts.py:216`), so nothing is broken at runtime. But a rebuild from migrations
produces a different database from the running one. That is a defect in Datum-Sync
independent of any redesign, and it should be fixed whatever is decided about the fable.

### Closing note — 2026-09-05

Fixed, in this order:

1. **Verified before recording.** The live `agents`, `proxy_log` and `mcp_call_log`
   were compared against `008`/`009` object by object — columns, types, defaults,
   indexes, CHECK constraints — *before* being stamped into `schema_migrations`, so
   the ledger records what is true rather than what is convenient. That the runner
   was genuinely broken was shown first, by piping 008 into a
   `BEGIN; ... ROLLBACK;` transaction: `ERROR: relation "agents" already exists`.
2. **`migrations/010_tier_five.sql`** records the 4→5 widening. `001` and `005` were
   *not* edited in place: they are applied and checksummed, so editing them would
   trip `migrate.py`'s drift check — correctly. A new migration is the only honest
   way to record a change to an applied schema.
3. **`tests/test_schema_drift.py`** (guard `SCHEMA-001`) builds a scratch database
   from `migrations/` by driving `migrate.migrate()` — the runner, not a replay of
   the .sql files, so the tracking table and the apply order are covered too — and
   diffs columns, constraints and indexes against the live database.

`migrate.py` could not have caught this on its own, and the reason is worth keeping:
its drift check compares a file's checksum against the checksum recorded when that
file was applied, which detects an **edited** migration and nothing else. A change
made with no migration leaves no file to checksum, and an unrecorded migration is
indistinguishable from one that has simply not run yet — `--status` printed
`[pending]` for two files whose tables already existed.

The new test was falsified in both directions before being trusted: an extra table
created in the live database made it fail naming both columns, and reverting the live
`connections_tier_check` to `tier <= 4` made it fail naming the exact original defect.

