# CLAUDE.md — Datum-Sync

## What this is

Datum-Sync is a workspace runner and hosting platform for data processing pipelines.
Python workspaces are published with a typed manifest, then callable via REST API, MCP
(Claude.ai, ChatGPT, Copilot), or a web UI. The platform handles scheduling, automation,
delivery, and persistent hosted services.

Full design spec: `spec/` directory. Read `spec/overview.md` first.

## Status

**Implementing.** Steps 1–8 of the build order are done; step 9 (publish gate)
is next. Build order is in `spec/overview.md` — follow it, don't skip ahead.

Three gates, all of which must pass before a step is called done:
`pytest -q` · `python tests/break_the_guard.py` · `python tests/browser_smoke.py`.
Stop the worker before running the suite — a live worker claims the tests' jobs,
and a job left queued afterwards silently *skips* the UI tests rather than
failing them, which looks like a clean run.

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

1. `manifest.json` validates against schema
2. All declared connections exist and are accessible at the required tier
3. `MANIFEST.md` present and non-empty
4. Optional smoke test exits 0

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
