# Datum Sync

Datum Sync is an authority plane for a fleet of AI agents. Agents get an identity, a
permission bundle and a governed route to tools; they never get the credentials
behind those tools. **Datum Sync contains no LLM and calls no model.** The model runs
in a small, replaceable worker. Datum Sync decides what that worker may do, injects
the secrets server-side, records every call as payload-free evidence, and can revoke
any of it without restarting anything.

```
worker (Codex, no secrets)  ──consent──▶  Datum Sync  ──governed MCP──▶  OfficeCLI · PostgreSQL · harness tools
        :8220                             :8210 portal                    upstream secrets stay here
                                          :8200 API / jobs / vault
```

Read [docs/architecture.md](docs/architecture.md) for the trust boundary, the request
path, the four revocation controls and the production gap list. The six-page
product overview is at [docs/walkthrough/output/Datum-Sync-Walkthrough.pdf](docs/walkthrough/output/Datum-Sync-Walkthrough.pdf).

## What it does

| Surface | What you get |
|---|---|
| **Agent principals** | Every agent is a first-class principal with an owner, lifecycle state, grant and audit trail. Enrolment is single-use and needs human approval. |
| **Consent and sessions** | A worker connects through OAuth consent. The operator picks the agent and a 15-minute, 2-, 4- or 8-hour session; the deadline is absolute. |
| **Governed MCP** | Operators register MCP servers once. Tools are discovered, enabled per tool with a minimum tier, and granted to agents by name. One `/mcp` endpoint serves everything. |
| **Managed credentials** | Upstream secrets are write-only, versioned, rotated only after a live probe, and injected at dispatch. Agents receive explicit, revocable use grants — never the value. |
| **Evidence** | Every MCP call leaves a deterministic timeline — receipt, authentication, session, authorization, dispatch, completion or denial — with no prompts, arguments or results. |
| **Governed resources** | Agents save versioned, private artifacts and share them with one named agent for a bounded time. Operators see ownership and shares and can revoke. |
| **Workspaces and jobs** | The original gateway: Python workspaces published as REST, MCP and web tools, with schedules, automations, hosted services and a path-scoped vault. |

## Quickstart

Requirements: Python 3.11+, PostgreSQL 16 with PostGIS and pgcrypto (the included
`docker-compose.yml` starts one on `:5435`).

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env                      # set PUBLIC_URL; DATABASE_URL matches docker-compose
docker compose up -d db
export DATUM_SYNC_SECRET_KEY_01=$(python -m datum_sync.crypto)   # seals managed credentials
export LEGACY_MCP_TOKEN=$(python -c 'import secrets;print(secrets.token_urlsafe(32))')
python -m datum_sync.migrate              # applies migrations 001–023
python -m datum_sync.portal_seed          # creates the operator; password goes to .runtime/operator.txt
uvicorn datum_sync.portal:app --host 127.0.0.1 --port 8210
```

`demo/manage.py start` generates these values for you and keeps them in
`.runtime/config.json`; set them by hand only when running the portal directly.

Sign in at `http://127.0.0.1:8210` as `operator`. For the full demonstration stack —
portal, worker, the legacy adapter and the demo MCP providers on a private
PostgreSQL cluster — use the launcher instead:

```bash
python demo/manage.py start
python demo/provision_personas.py --register     # seven persona agents with their grants
```

Then open the worker at `http://127.0.0.1:8220`, choose **Connect agent**, pick
**researcher** and a session length, and ask it to list the Office documents it can
access. `docs/prototype/INSTRUCTIONS.md` is the step-by-step demo runbook.

## Repository layout

```
datum_sync/          the service: API (api.py), portal (portal.py), MCP gateway (mcp.py, federation.py),
                     credentials (credential_access.py), evidence (mcp_observe.py), resources (resources.py),
                     jobs, schedules, automations, vault, hosted services; static-v2/ and portal_static/ UIs
migrations/          001–015 original schema · 016–023 agent auth plane (all additive)
tests/               pytest suite plus the standalone gates break_the_guard.py, browser_smoke.py, flow_geometry.py
worker/              the federated Codex worker: app.py, codex_bridge.py, datum_sync_client.py, vendored branding
demo/                manage.py (start/stop/test the local stack), provision_personas.py, personas.py, verify.py
docs/architecture.md Datum Sync vs the worker; production considerations
docs/walkthrough/    brochure build (OfficeCLI), demo video recorder (Playwright), screenshots, outputs
docs/prototype/      runbooks: INSTRUCTIONS, PERSONAS, WALKTHROUGH narration, INTEGRATIONS, IMPLEMENTATION
docs/review/         security review record and delivery summary
spec/                original design documents and the holonic framing
repositories/        sample workspaces (Hermes, SCIMAC, Testing)
deploy/              systemd units for the API and worker
```

## Auth model

Five tiers control what a principal — human or agent — can do:

| Tier | Role | Access |
|------|------|--------|
| 1 | Read-only | Authenticate, read public state |
| 2 | Internal | Read internal connections and vault paths; MCP catalogue |
| 3 | Read/write | Execute workspaces, write to scoped vault paths; `mcp:operate` |
| 4 | Admin | Manage accounts, connections, automations, MCP servers |
| 5 | Superuser | Full platform access, no scope restrictions |

A grant can only narrow what a tier allows. Elevation is a separate, time-boxed
OAuth device-flow request that a human approves; refresh cannot extend its deadline.

## Testing

```bash
pytest -q                                   # service suite (needs the database)
python tests/break_the_guard.py             # proves each guard is load-bearing
python tests/browser_smoke.py               # Playwright, needs a running server
python tests/flow_geometry.py               # layout parity with the reference UI
pytest worker/tests                         # the worker, no database needed
python demo/manage.py test                  # portal + worker suites on a disposable database
```

Stop the worker before running the service suite; a live worker claims the tests'
jobs. `CLAUDE.md` explains the gates and what a skipped test means.

## Design principles

- **The gateway enforces, the agents think.** No model inside Datum Sync; no
  credentials inside the worker.
- **Everything is a principal.** Humans and agents register the same way and are
  governed by the same tier, grant, session and audit records.
- **Deny by default, revoke immediately.** Tools, servers, grants and agents are
  separate controls; each takes effect on the next call.
- **Evidence without payloads.** The audit trail proves what happened without
  storing what was said.
- **Holonic.** Datum Sync is the head holon of a moderated group; each agent runtime
  is a member with local autonomy inside the boundary it was granted. See
  [`PROPOSAL.md`](PROPOSAL.md) and [`spec/holonic-shacl-analysis.md`](spec/holonic-shacl-analysis.md).

## Architecture

FastAPI, PostgreSQL + PostGIS, `asyncpg` with no ORM. MCP Streamable HTTP with OAuth
2.1 PKCE and device flow. `pg_notify` for durable SSE and job events. Hand-written
vanilla-JS UIs. AES-256-GCM sealed secrets with key-id prefixes. Subprocess-isolated
workspace execution. Pinned-IP egress for every credentialed outbound call.
