# Datum-Gate corpus — what this is and how it was reviewed

**Source:** `~/vault/_inbox/2026-09-05/Datum-sync Claude Fable.zip` (preserved here
as `_original.zip`). 24 documents, ~33k words. A complete alternative design for the
same problem Datum-Sync solves: a multi-tenant agent gateway. Working name
**Datum-Gate**, package `datumgate`. It is a paper corpus — no implementation exists.

**Reviewed:** 2026-09-05. All 24 documents read. Four files sit beside them:

| File | Covers |
|---|---|
| `_REVIEW-decision-log-audit.md` | Every claim the fable makes *about Datum-Sync*, checked against our source |
| `_REVIEW-docs-02-13.md` | Documents 02–13 (domain model → hosted services) |
| `_REVIEW-docs-14-23.md` | Documents 14–23 (audit → tenancy), incl. the guard-ID audit |
| `_BACKPORT-PLAN.md` | What to merge into this repo, in what order, and what not to |

## Corpus completeness

`00-README.md` lists `24-future-improvements.md` in its reading order.
**That file is not in the zip.** Either the export dropped it or it was never written.

## Verdict in one paragraph

Documents 01–20 are a genuinely rigorous spec — verbatim SQL for the four
concurrency paths a job engine actually gets wrong, byte-level sealed-blob layout,
`dup2` ordering specified relative to workspace import, ~130 registered guards each
naming an exact string to delete and the test that must then fail. It is buildable
with a one-page errata. Documents 21–23 (federation, memory, lifecycle, tenancy) —
which the README instructs you to read first as the org-level vision — are a
**proposal with a schema sketch**, not a spec. That is also, unintentionally, an
accurate warning: the vision and the thinnest engineering are in the same documents.

## The defects that matter most

**In the fable's own design:**

1. **`08 §9` — the workspace/vault trust boundary is asserted, not built.** A job is
   claimed not to exceed its frozen vault slice; enforcement is a helper the
   workspace *imports voluntarily*, in a subprocess with no sandbox by default.
   Guard VAULT-021 passes while the property is false. The entire delegated-drone
   story in `11 §8` rests on this. Needs a real sandbox or an honest downgrade.
   In a corpus built on the premise that it knows the difference between a test and
   evidence, this is the one thing not to ship as written.

2. **Argument guards cannot express their own flagship example.** `argument_path` is
   a single dotted path; the GitHub profile needs `owner` + `repo` joined as
   `owner/repo` to match a `repos: ["datum/*"]` grant. As written the most important
   guard in the most important profile denies everything. Also: `paths` is not a
   valid grant field, list-of-object arguments can't be addressed, and
   `resources/read` federates with **no guards at all** — a federated Drive or code
   server exposing file content as resources is reachable with no folder check.

3. **Version rollback does not work.** Versions are called immutable and
   hash-addressed, but no bytes are stored; `activate` requires the disk to still
   match. Two documents describe a capability the schema cannot deliver — and
   rollback-by-pointer-flip is one of the headline advantages over Datum-Sync.

4. **Scheduled jobs get an empty vault scope and empty proxy list**, because
   `intersect` meets the owner's grant against a seeded `system:worker` grant with
   empty arrays. A one-line consequence of two documents written apart that silently
   disables a whole category of intended use.

5. **Later documents amended the model; earlier ones were never revisited.**
   `10-rest-api.md` ("every HTTP endpoint") has **zero** occurrences of memory,
   review, register, teams or activity. `11-mcp.md` has zero occurrences of memory
   or federation. `narrows()`/`intersect()` in `03` cover five of ten grant blocks.
   `16`'s `policy.yaml` is missing three blocks that 21–23 require — and an invalid
   policy file is a documented startup refusal, so the corpus as shipped would not
   boot. The `00-README` claim that every document is self-contained is false in at
   least five places.

**In the fable's account of us:** three claims are flatly false (D3, D19, D36) and
one is materially misleading (D17). See `_REVIEW-decision-log-audit.md`.

**In Datum-Sync, found while checking — since FIXED:** our migrations no longer
described our database. Checked-in files said `CHECK (max_tier BETWEEN 1 AND 4)`
while the live DB said 1–5, with no migration making the change, and 008/009 sat
unapplied despite their tables existing. Closed on 2026-09-05: 008 and 009 stamped
after verifying the live tables match their files object by object,
`010_tier_five.sql` written to record the widening, and `tests/test_schema_drift.py`
added — it builds a scratch database from `migrations/` and diffs columns,
constraints and indexes against the live one, which is the only check that could
have caught this. Unrelated to the redesign.

## What is worth taking

Ranked by value against implementation cost, on the evidence above:

1. **The guard registry format** (`17`) — stable IDs, an exact `remove:` string, and
   a **skipped test counts as UNPROVEN**. That last clause closes the exact failure
   mode this project has hit before. Cheap; adopt independent of everything else.
2. **One `audit_log` + a server-minted trace id** (D14/D15) — replaces four tables
   and a client-supplied, untrusted header.
3. **Single grant document** (D2) — removes the deliberate NULL asymmetry.
4. **Multi-key secrets with a key-id byte** (D16) — rotation without downtime.
5. **Multiple labelled tokens per principal** (D5) — a real fleet gap today.

Note that 1, 2, 4 and 5 are all backportable into Datum-Sync without adopting the
redesign. The federation and memory layers (the genuinely new ideas) are the parts
that are not buildable as written.
