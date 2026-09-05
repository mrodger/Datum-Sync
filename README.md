# Datum-Sync

Multi-tenant agent gateway. Human operators and AI agents authenticate the same way, get tiered access to workspaces, credentials, and vault paths, and execute jobs through a single orchestration layer.

**Datum-Sync contains no LLM and calls no model.** It runs subprocesses (FME, Python, shell), enforces auth via bearer tokens and glob-matched vault scopes, and logs everything. The intelligence is in the agents. The gateway enforces rules.

## Status

**Built — 10 implementation steps complete, 406+ tests, 112 guards.** Running on VM112 at `:8200`.

Three registered principals: `Marcus` (tier 4 admin), `superuser` (tier 5), `harness-researcher` (tier 2 agent). Agents register the same way humans do — just a different tier of auth.

## What it does

Datum-Sync publishes Python workspaces as MCP-callable tools — accessible via REST API, MCP Streamable HTTP, or a web UI. Workspaces declare their inputs, outputs, and connection requirements in a manifest. The platform handles auth, scheduling, automation, delivery, and hosted service proxying.

## Auth model

Five tiers control what a principal (human or agent) can access:

| Tier | Role | Access |
|------|------|--------|
| 1 | Read-only | Can authenticate, read public state |
| 2 | Internal | Read internal connections and vault paths |
| 3 | Read/write | Execute workspaces, write to scoped vault paths |
| 4 | Admin | Manage accounts, connections, automations |
| 5 | Superuser | Full platform access, no scope restrictions |

Accounts and agents are managed via the Admin panel or REST API. Revoke drops a principal to tier 1 (reversible); Delete removes permanently.

## Key concepts

- **Workspaces** — Python modules with a typed parameter interface and structured output
- **Connections** — scoped, tiered credential store (database, HTTP, email, file) with AES-GCM encryption bound to connection name as AAD
- **Automations** — YAML-configured triggers and actions (schedule, webhook, email)
- **Hosted services** — publish a workspace output as a persistent web service at `/serve/{name}/`
- **MCP endpoint** — expose workspaces as tools callable from any MCP-compatible AI client
- **Vault gate** — path-scoped NFS vault access with full audit log, pySHACL coherence validation
- **MCP call log** — uniform audit spine for all MCP requests across all principals
- **Credential proxy** — two-level identity with auth injection and SSRF guard

## Architecture

- FastAPI backend, PostgreSQL + PostGIS (`:5435`)
- MCP Streamable HTTP transport with OAuth 2.0 PKCE
- APScheduler for cron-based triggers
- `pg_notify` for durable SSE streaming
- Vanilla JS web UI with account/agent management
- pySHACL for vault_scope coherence validation at account write time

## Holonic design

Datum-Sync is the **Head holon** in a Moderated Group holarchy. Each agent VM is a Member holon with full local autonomy for operations that don't cross the boundary. The `service_accounts` table is the membership registry; `vault_scope` is the boundary graph.

See [`PROPOSAL.md §4`](PROPOSAL.md) and [`spec/holonic-shacl-analysis.md`](spec/holonic-shacl-analysis.md) for the full framing.

## Running

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn datum_sync.api:app --host 0.0.0.0 --port 8200
```

`datum_sync.runner` is the job runner, not the server — it is imported by the
worker and has no `__main__`.

Database: PostgreSQL + PostGIS on `:5435`. Apply migrations in order: `migrations/001_*.sql` → `migrations/006_*.sql`.
