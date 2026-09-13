# Agent Auth Plane + MCP Gateway — delta specification

**Date:** 2026-09-13
**Status:** draft for review, not yet handed to a worker
**Applies to:** `mrodger/Datum-Sync` at commit `d2d1b2c` (C3, rate limit)

## What this is

A specification for turning Datum-Sync into the product described in one
sentence:

> A production-grade authentication plane and MCP gateway through which many
> people and many AI agents reach company resources. A registered agent runs
> in a **baseline** configuration on its own (one session at a time, a narrow
> grant, its own namespace). To reach broader services it must connect to the
> gateway over MCP and elevate through OAuth, with a human in the loop.

It is a **delta spec against the code that exists**, not a third re-design.
The repository already carries two design documents:

| Document | What it is | How this spec treats it |
|---|---|---|
| `spec/overview.md`, `spec/components.md`, `spec/workspace-contract.md` | The original Datum-Sync build spec. Steps 1–10 are built. | Unchanged. Nothing here alters the workspace contract, the job engine or hosted services. |
| `spec/datum-gate/*` | A 23-document alternative design ("Datum-Gate") plus four review files and a backport plan. B1–B5 and C1–C3 of that plan are merged. | Source of the authority, lifecycle, federation and tenancy ideas. Where a Datum-Gate document is reused, this spec says so and states what was changed to make it buildable. Its known defects (`_README-review.md`) are fixed here, not repeated. |

The backport plan's standing decision — **merge into this repository, no
`datumgate` rewrite** — is kept. Every change below is a migration on the
live schema plus a change to a module that already exists.

## Reading order

1. `01-assessment.md` — what the code does today, measured against the vision; the defects found while reading it. Read this first even if you know the codebase; several claims in `README.md` are not what the code does.
2. `02-target-architecture.md` — the product, its two agent modes, trust boundaries, the request flows, and the decisions that shape everything else.
3. `03-principals-lifecycle-sessions.md` — one principal table, grants that narrow, lifecycle states, enrolment, MCP sessions and the concurrency limit.
4. `04-oauth-elevation.md` — OAuth scopes that mean something, the device flow for headless agents, consent bound to the right principal.
5. `05-mcp-gateway-federation.md` — upstream MCP servers as connections, catalogue federation, an argument-guard grammar that can express the GitHub case, approval-gated calls.
6. `06-schema.md` — the migrations, in order, as DDL.
7. `07-api-surface.md` — every new or changed REST endpoint and MCP method.
8. `08-web-ui.md` — the screens, against `static-v2`.
9. `09-build-plan.md` — work packages sized for a worker session, each with an acceptance gate and its guard ids.
10. `10-worker-brief.md` — the brief to hand a worker model with any single work package.
11. `11-decision-log.md` — every choice made here that a reader might want to relitigate, with the reason.

## Conventions

- MUST / MUST NOT / SHOULD carry RFC 2119 meaning.
- File and symbol references are to this repository at the commit above: `datum_sync/auth.py:resolve`.
- Guard ids follow the registry in `tests/break_the_guard.py` (`AUTH-`, `PROXY-`, `VAULT-` …). New families introduced here: `PRIN-` (principals and grants), `SESS-` (sessions), `ELEV-` (OAuth elevation), `ENRL-` (enrolment), `FED-` (federation), `TIER-` (verb ceilings).
- "Principal" is any authenticated caller. "Agent" is a principal of `kind='agent'`. "Sponsor" is the principal that created an agent. "Baseline" and "elevated" are the two access levels defined in `02 §3`.
- The four gates in `CLAUDE.md` apply to every work package. A skipped test is not a pass.
