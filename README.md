# Datum-Sync

Multi-tenant agent gateway. Human operators and AI agents authenticate the same way, get tiered access to workspaces, credentials, and vault paths, and execute jobs through a single orchestration layer.

**Datum-Sync contains no LLM and calls no model.** It runs subprocesses (FME, Python, shell), enforces auth via bearer tokens and glob-matched vault scopes, and logs everything. The intelligence is in the agents. The gateway enforces rules.

## Status

**Built — 10 implementation steps plus the agent auth plane (`spec/agent-auth-plane/`), 800+ tests, 240+ guards.** Deployed on VM112 at `:8201` (see `CLAUDE.md` for the layout, services and redeploy steps).

Humans and agents are rows in one table. An agent is enrolled by a sponsor's code, runs in a **baseline** configuration on its own token (one session, a narrow grant, tier 2), and reaches more by connecting over MCP and elevating through OAuth with a person in the loop: the consent page for Claude Code and Codex, the device flow for anything headless. Upstream MCP servers (GitHub, SSH, Drive, anything streamable-HTTP) are connections whose tools are federated into the one catalogue behind argument guards.

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
- **Audit log** — one table, one row per authorised action, joined by a server-minted trace id and grouped by MCP session
- **Credential proxy** — two-level identity with auth injection and SSRF guard
- **Principals** — one table for humans and agents; grants narrow down the delegation tree; lifecycle states; enrolment by code; review queue
- **Elevation** — OAuth scopes cap the effective tier; consent scope picker, `on_behalf_of`, RFC 8628 device flow; approvals screen
- **Federation** — upstream MCP servers as connections; cached catalogue; argument guards (`owner/repo`, hosts, commands, folders); approval-gated calls

## Architecture

- FastAPI backend, PostgreSQL + PostGIS (`:5432` native on the VM; `:5435` in the dev `docker-compose.yml`)
- MCP Streamable HTTP transport with OAuth 2.0 PKCE
- No scheduler object: the worker polls `schedules.next_run` in the database, so a restart loses nothing
- `pg_notify` for durable SSE streaming
- Vanilla JS web UI (one shell at `/ui/v2`, no build step) with principals, enrolment, approvals, review and federation screens
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

Database: PostgreSQL + PostGIS (`docker-compose up -d` gives one on `:5435`). Apply every migration in order with `python -m datum_sync.migrate`; `--status` lists what is applied. Migrations are checksummed and `tests/test_schema_drift.py` proves the live schema matches them.
