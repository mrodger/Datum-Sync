# Datum-Sync

Deterministic agent gateway and workspace runner. Every agent (Datum, OpenClaw, Hermes, research drones) connects here for vault access, job execution, tool discovery, credential management, and hosted service proxying.

**Datum-Sync contains no LLM and calls no model.** It runs subprocesses (FME, Python, shell), enforces auth via bearer tokens and glob-matched vault scopes, and logs everything. The intelligence is in the agents. The gateway enforces rules.

## Status

**Built — 10 implementation steps complete, 406 tests passing, 112 guards.** Deployed to VM112 at `:8200` but not yet running in production.

Phase 3 planning (Agent Hub) is in [`PROPOSAL.md`](PROPOSAL.md). Holonic architecture analysis in [`spec/holonic-shacl-analysis.md`](spec/holonic-shacl-analysis.md).

## What it does

Datum-Sync publishes Python workspaces as MCP-callable tools — accessible via REST API, MCP Streamable HTTP, or a web UI. Workspaces declare their inputs, outputs, and connection requirements in a manifest. The platform handles auth, scheduling, automation, delivery, and hosted service proxying.

## Key concepts

- **Workspaces** — Python modules with a typed parameter interface and structured output
- **Connections** — scoped, tiered credential store (database, HTTP, email, file) with AES-GCM encryption
- **Automations** — YAML-configured triggers and actions (schedule, webhook, email)
- **Hosted services** — publish a workspace output as a persistent web service
- **MCP endpoint** — expose workspaces as tools callable from any MCP-compatible AI client
- **Vault gate** — path-scoped NFS vault access with full audit log (Phase 1, next)

## Architecture

- FastAPI backend, PostgreSQL + PostGIS (`:5435`)
- MCP Streamable HTTP transport with OAuth 2.0 PKCE
- APScheduler for cron-based triggers
- `pg_notify` for durable SSE streaming
- Vanilla JS web UI
- pySHACL for vault_scope coherence validation at account write time

## Holonic design

Datum-Sync is the **Head holon** in a Moderated Group holarchy. Each agent VM is a Member holon with full local autonomy for operations that don't cross the boundary. The `service_accounts` table is the membership registry; `vault_scope` is the boundary graph.

See [`PROPOSAL.md §4`](PROPOSAL.md) and [`spec/holonic-shacl-analysis.md`](spec/holonic-shacl-analysis.md) for the full framing.

## Running

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m datum_sync.runner
# Serves on http://0.0.0.0:8200
```

Database: PostgreSQL + PostGIS on `:5435`. Apply migrations in order: `migrations/001_*.sql` → `migrations/006_*.sql`.
