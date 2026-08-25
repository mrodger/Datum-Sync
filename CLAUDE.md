# CLAUDE.md — Datum-Sync

## What this is

Datum-Sync is a workspace runner and hosting platform for data processing pipelines.
Python workspaces are published with a typed manifest, then callable via REST API, MCP
(Claude.ai, ChatGPT, Copilot), or a web UI. The platform handles scheduling, automation,
delivery, and persistent hosted services.

Full design spec: `spec/` directory. Read `spec/overview.md` first.

## Status

**Implementing.** Steps 1–9 of the build order are done; step 10 (hosted
services) is next. Build order is in `spec/overview.md` — follow it, don't skip
ahead.

Three gates, all of which must pass before a step is called done:
`pytest -q` · `python tests/break_the_guard.py` · `python tests/browser_smoke.py`.
Stop the worker before running the suite — a live worker claims the tests' jobs,
and a job left queued afterwards silently *skips* the UI tests rather than
failing them, which looks like a clean run.

**A skipped test reads as a passing one.** `break_the_guard.py` asks whether a
test fails when a guard is deleted; a skip is not a failure, so a poisoned queue
reports the guards *unproven* rather than reporting the queue. If a run comes
back with guards unproven in bulk, check `pytest -q` for skips before believing
any of them. An API server also queues jobs — it runs the scheduler, so one left
running quietly refills the queue from any enabled schedule, and no worker means
those jobs stay `queued` forever.

## Running it for hand-testing

`DATUM_SYNC_AUTH=off` serves every request as an administrator with no
credential, so the UI can be driven without provisioning accounts. It requires
`HOST=127.0.0.1` — the server refuses to start otherwise — and separately checks
the connecting socket's own address on every request, because `uvicorn --host`
binds without consulting `HOST` and would otherwise walk around the startup
check. With it on, the account area of the UI shows a red **NO AUTH** badge.

Accounts are managed by `python -m datum_sync.accounts`
(`create` · `token` · `passwd` · `disable` · `enable` · `list`). There is no HTTP
route for it, which is deliberate. Give the smoke account a password with
`passwd`; do not write to `service_accounts` directly, because the CLI hashes
with the argon2 settings the login path verifies against.

## Target environment

- Host: Stratum VM (192.168.88.112), isolated from datum-ui
- Public access: geofabnz tunnel (HTTPS)
- MCP endpoint: `https://geofabnz.com/mcp`
- Database: PostgreSQL + PostGIS, dedicated instance on vm112
- Python: 3.12
- Framework: FastAPI

## Locked design decisions

These are settled. Don't relitigate without a reason:

| Decision | Value |
|---|---|
| REST prefix | `/rest/v1/` |
| Service paths | `/stream/`, `/download/`, `/upload/` (no prefix) |
| Hosted services path | `/serve/{name}/` |
| MCP transport | Streamable HTTP |
| MCP auth | OAuth 2.0 PKCE — hand-rolled in `oauth.py`, no `authlib` |
| Job streaming | pg_notify → SSE (durable, restart-safe) |
| DB | asyncpg against PostgreSQL + PostGIS |
| Scheduler | none — the worker polls `next_run` in the DB (no timer, no scheduler object, restart-safe) |
| Connection secrets | AES-256-GCM, key from `DATUM_SYNC_SECRET_KEY`, connection name as AAD |
| Default timezone | Pacific/Auckland |
| Bearer token storage | `sha256(token)` only — raw value never persisted |
| Workspace isolation | subprocess, not import |
| Automation config | YAML (no visual builder) |

## Workspace contract

See `spec/workspace-contract.md` for full detail.

```python
async def run(params: dict, emit, connections: dict) -> list[dict]:
    await emit("progress", {"pct": 0.0, "message": "Starting"})
    # ...
    return [{"name": "report", "type": "text/html", "content": "..."}]
```

Every workspace needs: `main.py`, `manifest.json`, `MANIFEST.md`.

## Publish gate (all must pass)

`datum_sync/publish.py`, run from `repository.sync()`. Cheapest check first — a
workspace with a broken manifest never costs a process launch.

1. `manifest.json` validates against schema (`manifest.load_manifest`)
2. Every declared connection exists, and is in scope for `repository/workspace`
   — the same predicate `connections.resolve` uses at run time
3. The **publisher's** `max_tier` covers every connection's tier. Checked
   against whoever publishes, not whoever later submits a job: the workspace
   runs with its own authority, and the submitter never chose its connections
4. `MANIFEST.md` present with all five sections **non-empty** — "N/A", "TBD" and
   an HTML comment all count as empty
5. Smoke test exits 0, if `manifest.json` sets `"smoke_test": true`. Opt-in
   because a workspace with no `--smoke` handler ignores the flag and runs for
   real, so an always-on check would report a pass having done the side effects

Failing the gate drops the workspace from what gets written — the previously
published version stays up and callable. Stale is computed from the **disk**
before the gate runs, so a broken workspace is never mistaken for a removed one.

`python -m datum_sync.repository sync [--dry-run] [--prune] [--as ACCOUNT]`.
Without `--as` it publishes as an unrestricted local admin, which is honest:
anyone who can run it already has the database URL and the decryption key.
`--dry-run` skips the smoke test — it promises to change nothing.

## Connection tier model

- Tier 1: public or low-sensitivity (email send, public HTTP)
- Tier 2: internal data sources (read-only DB, SharePoint)
- Tier 3: internal data sources (read-write DB)
- Tier 4: admin / secrets (full access)

Service account `max_tier` must be >= connection tier to use it.

## Reference implementation

SCIMAC Ltd — `spec/case-study-scimac.md`. Use this as the test harness for every
component: if SCIMAC can run their workflows end-to-end, the component is working.

## Coding standards

- Minimum code that solves the problem. No speculative features.
- Every new agent/worker module needs a `MANIFEST.md`.
- No hardcoded credentials. Connections inject resolved objects; credentials never
  appear in environment variables or logs.
- DB access via asyncpg only. No ORM.
- Match existing style. Don't clean up adjacent code you weren't asked to touch.

## Key file map (to be populated as build progresses)

| File | Purpose |
|---|---|
| `spec/overview.md` | Goals, principles, build order |
| `spec/components.md` | All 12 components — schemas, API surface, data shapes |
| `spec/workspace-contract.md` | Workspace interface, manifest schema, publish gate |
| `spec/case-study-scimac.md` | Reference implementation |
