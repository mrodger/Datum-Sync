# 10 — Worker brief

Hand this document, `00-README.md`, the numbered document for the package,
and `09-build-plan.md` to a worker model together with the work package
name. Everything else it needs is in the repository.

---

You are implementing **work package WP<N>** of `spec/agent-auth-plane/` in
the `Datum-Sync` repository. Read, in order: `CLAUDE.md`,
`spec/agent-auth-plane/00-README.md`, `01-assessment.md` §4 and §6, the
documents your package cites, and `09-build-plan.md` for your package's
acceptance list. Then read the source files your package touches in full
before editing any of them. Read `tests/break_the_guard.py`'s header and
`tests/test_guard_registry.py` before adding a guard.

## How this repository works

- **Four gates, all green, before you call the package done:** `pytest -q`,
  `python tests/break_the_guard.py <your prefixes>`, `python
  tests/browser_smoke.py`, `python tests/flow_geometry.py`. The last two
  need a running server with `DATUM_SYNC_AUTH` **on** and a smoke account
  (`python -m datum_sync.accounts create … && passwd …`). Stop the worker
  process before `pytest`; a live worker claims the tests' jobs and the UI
  tests then *skip*, which reads as a pass. Check `pytest -q` for `s`
  before believing a clean run.
- **A guard is not done until it has been seen to fail.** Every security
  check you add gets a `CASES` entry: an exact source anchor that occurs
  once, its replacement, and a test carrying `Guard: <ID>` that fails when
  the anchor is replaced. Run `break_the_guard.py <ID>` and paste the
  output in your report. UNPROVEN is a bug in your test, not a note.
- **Migrations** are numbered, checksummed and never edited after they are
  applied. Each carries a comment block saying why it exists, what it
  changes, and how to revert. `tests/test_schema_drift.py` must pass.
- **asyncpg, no ORM. No new dependencies.** JSONB arrives as a string on
  this pool; decode it where `auth.vault_scope_of` does, not at the call
  site.
- **Only `sha256(token)` is stored, ever.** Raw credentials are returned
  once from the call that minted them and appear in no log, no audit
  `detail`, no error message.
- **Deny wins, absent means none, `*` is spelled.** A NULL scope grants
  nothing. A missing guarded argument is a deny. A pattern list that was
  never filled in reaches nothing.
- **One function per check.** Tier goes through `auth.require_tier`;
  narrowing through `grants.narrows`; audit through `audit.write` with an
  explicit `Trace` argument and no default. If you find yourself writing a
  second copy of a check, stop and thread the first one through.
- **Comments record what was measured.** The existing modules explain the
  reason for every non-obvious line and say when a value was checked
  against the database or the wire rather than assumed. Match that. Do not
  narrate what the code does; say why it is shaped that way and what
  breaks if it is changed.
- **Match the style around you.** No reformatting of lines you did not
  need to change. No cleanup of adjacent code. Vanilla JS in `static-v2/`
  with the `el()` helper and no `innerHTML`; new screens set
  `view.dataset.ready`.

## What to produce

1. Code, migrations, tests, guard entries, UI screens named in the package.
2. A report at `spec/agent-auth-plane/_report-WP<N>.md` with: what was
   built (file list); each acceptance item from `09` with the command that
   demonstrated it and its output excerpt; the `break_the_guard.py` output
   for your prefixes; anything in the spec that turned out to be wrong or
   underspecified, with what you did instead and why — and the same change
   appended to `11-decision-log.md`.
3. One commit per coherent step, messages in the repository's existing
   voice (`feat(auth): …`, first line under 72 characters, body explaining
   the reason). No model names in commits.

## What to do when the spec and the code disagree

The code is the truth about the present; the spec is the intent. If the
spec describes something the code cannot do as written (a column that
does not exist, a function with a different signature), do the smallest
change that meets the intent, and record it. If the spec asks for
something you believe is unsafe or wrong, do not build it silently and do
not skip it silently: build the rest of the package, write the concern in
the report with a proposed alternative, and stop there.

## Definition of done

- Every acceptance bullet in `09` for the package demonstrated.
- Every guard id in the package proven (fails when broken, passes when
  restored), cross-referenced both ways by `test_guard_registry.py`.
- Four gates green on a clean checkout with the worker stopped.
- No `s` in `pytest -q` that was not there before the package.
- Report written. Decision log updated if anything changed.
