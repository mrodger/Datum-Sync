# Datum-Sync — Phase 3 Proposal: Multi-Agent Vault Scoping

> **Status:** Draft for peer review  
> **Date:** 2026-08-28  
> **Author:** Datum agent (developer persona), per conversation with Marcus

---

## 1. The Vision in One Sentence

Datum Sync is the **deterministic infrastructure layer** that every agent (Datum, OpenClaw, Hermes, research drones) connects to for vault access, job execution, tool discovery, credential management, and hosted service proxying. It contains no model, calls no LLM, and evaluates no agent intent. The intelligence is in the agents. The gateway enforces rules, runs scripts, and logs everything.

---

## 2. Principles

1. **Datum Sync contains no LLM and calls no model.** It runs jobs (subprocesses: FME, Python, shell), validates parameters against declared schemas, enforces auth via bearer tokens and glob-matched vault scopes, and logs everything. No prompt, no inference, no reasoning. Governance is structural: rate limits, scope enforcement, audit logs. Agents reason; the hub enforces.
2. **Agents keep local autonomy.** Short-lived file reads, code execution, shell — all handled locally. The hub gates production data and destructive operations.
3. **MCP is the primary agent-facing protocol.** Discovery (`tools/list`), submission (`tools/call`) and result delivery all happen through MCP Streamable HTTP. No custom client needed for any agent type.
4. **Jobs are fire-and-forget.** The MCP response returns a `job_id` and `status: accepted`. Results land in the vault's mailbox or a Syncthing-shared directory. Agents check for results when they need them.
5. **One token per agent, scoped to its tier.** The hub holds the credential model. Agents carry bearer tokens with path scopes, repo scopes, and connection grants. No shared keys.
6. **The proxy layer unifies the PWAs.** Catalogue, Shots, Drone Monitor, and future apps live behind Datum Sync on a single domain with auth where needed.

---

## 3. What Datum Sync Is Not

This is worth stating explicitly because the surrounding architecture is LLM-heavy.

| It is NOT | It IS |
|---|---|
| An AI agent | A job engine that runs subprocesses |
| An LLM wrapper or orchestrator | A deterministic auth + dispatch layer |
| A reasoning system | A rule enforcer (scopes, schemas, rate limits) |
| A model routing layer | An MCP server that exposes tools to agents |
| A replacement for agent intelligence | Infrastructure agents call into |

The SHACL shapes used in vault_scope validation (§6.1.2) are structural constraints — they run as a deterministic graph check, not an inference engine. pySHACL is a validator, not a reasoner.

---

## 4. Holonic Architecture

Datum Sync is a **Head holon** in a Moderated Group holarchy (Koestler/W3C Holon CG framing). This is descriptive, not prescriptive — the code does not change.

| Holon component | Datum Sync implementation |
|---|---|
| Knowledge graph | `repositories` + `workspaces` + `workspaces.manifest` — the tool catalogue |
| Context/event graph | `jobs` + `job_log` + `automation_runs` — execution history |
| Boundary graph | `service_accounts.vault_scope` — what each agent is permitted |
| Projections | `mcp.py → catalogue()` — per-principal filtered `tools/list` output |

Each agent VM is a **Member holon**: full local autonomy for operations that don't cross the boundary. A `service_accounts` row is the membership record. Authority flows down: a research drone's vault scope is the intersection of its account scope and the dispatching agent's delegated slice — it cannot exceed its parent's grant (see §6.1.2).

Full analysis: `spec/holonic-shacl-analysis.md`.

---

## 5. Current State (Datum 2.0)

### What exists

| Component | Status | Notes |
|---|---|---|
| **Datum Sync codebase** | 26 modules, 6 migrations, production-quality MCP endpoint | Deployed to VM112 at `:8200` but not running in production |
| **Datum Sync DB** | Separate PostGIS instance on `:5435` | 2 repos, 5 workspaces, 71 completed jobs, 476 log entries |
| **Orchestrator DB** | PostGIS 16 on `:5433` | 841 convos, 30k messages, 47k brain files, 49k forensics tool calls |
| **Vault graph tools** | `vault-mcp.py` — graph search, note, backlinks, tags | Part of datum-local MCP. No auth, no scope, no audit. |
| **Vault** | 3.4TB NFS share mounted on VM102, VM111, VM112 | /vault/dev/, /vault/shared/, /vault/quarantine/, /vault/_inbox/ |
| **Catalogue** | PWA on VM102:3027 | Search over vault + tools index |
| **Shots** | PWA on VM102:8330 | Session screenshot viewer with service worker |
| **Pipeline** | Vite dev on VM102:5274 | Prototype, deferred |
| **Drone Monitor** | HTTPS on VM111:3025 | Self-signed cert. Currently an MCP tool, not a standalone UI. |
| **Syncthing** | VM102 ↔ Stanger Bridge (192.168.88.101) | Shared folder at `~/shared-stanger/` |

### What doesn't exist yet

- Vault access through Datum Sync (scope enforcement, audit)
- Hosted service proxy (reverse proxy for PWAs)
- Cross-DB integration (orchestrator DB + Datum Sync DB don't talk)
- Production deployment (no systemd, no Docker Compose, no health checks)
- Agent MCP connections (no agent currently calls Datum Sync's MCP endpoint)

---

## 6. Target Architecture

```
                           ┌──────────────────────────────────────┐
                           │            Datum Sync (VM112)         │
                           │                                       │
  Agent ──MCP──►           │  ┌─────────────┐  ┌──────────────┐   │
                           │  │  MCP Server  │  │  Job Engine   │   │
                           │  │  /mcp        │  │  workers +    │   │
  Datum (VM102)            │  │  tools/list  │  │  subprocess   │   │
  OpenClaw (VM102)  ──MCP──►  │  tools/call  │  │               │   │
  Hermes             ──MCP──►  │              │  │  FOR UPDATE   │   │
  Research Drones           │  │  auth/scope  │  │  SKIP LOCKED  │   │
  (VM111)            ──MCP──►  └──────┬───────┘  └──────┬────────┘   │
                           │         │                  │           │
                           │  ┌──────┴───────┐  ┌──────┴────────┐  │
                           │  │  Vault Gate   │  │  Hosted       │  │
                           │  │  /mcp tools:  │  │  Services     │  │
                           │  │  vault_read   │  │  Proxy        │  │
                           │  │  vault_write  │  │               │  │
                           │  │  vault_search │  │  catalogue    │  │
                           │  │  quarantine   │  │  shots        │  │
                           │  │  promote      │  │  drones       │  │
                           │  └──────┬───────┘  └──────┬────────┘  │
                           │         │                  │           │
                           │  ┌──────┴───────┐         │           │
                           │  │  Auth +       │         │           │
                           │  │  OAuth 2.0    │         │           │
                           │  │  PKCE         │         │           │
                           │  │  Bearer +     │         │           │
                           │  │  Session      │         │           │
                           │  │  Scoped       │         │           │
                           │  │  vault_scope  │         │           │
                           │  └───────────────┘         │           │
                           └────────────────────────────┼───────────┘
                                                        │
                              ┌─────────────────────────┼──────────┐
                              │                         │          │
                              ▼                         ▼          ▼
                    ┌─────────────────┐    ┌─────────────────┐
                    │    NFS /vault/   │    │  VM102:3027      │
                    │  (Node 1 RAID)   │    │  Catalogue       │
                    │                   │    │  VM102:8330      │
                    │  dev/            │    │  Shots           │
                    │  shared/         │    │  VM111:3025      │
                    │  quarantine/     │    │  Drone Monitor   │
                    │  _inbox/         │    │                  │
                    │  logs/           │    │  (proxied, not   │
                    │  mailbox/        │    │   direct access) │
                    └─────────────────┘    └─────────────────┘
```

### Data flow: vault read

```
Agent                     Datum Sync                        NFS
─────                     ──────────                        ───
tools/list ──────────────► returns vault_read tool
                          (visible if token scoped)

tools/call(vault_read,    auth check
  path=dev/config/foo)──► vault_scope: path MUST match
                          read: ["dev/**", "shared/long_term/**"]
                          enforce match

                          read /vault/dev/config/foo ──────────► file
  ◄── content             log to job_log:                          write audit
                          {"workspace": "vault_read",              record
                           "trace": "..."}
```

### Data flow: job execution (fire-and-forget)

```
Agent                     Datum Sync                        Worker
─────                     ──────────                        ──────
tools/call(site_plan,     auth + param validation
  JOB_ID=70023) ────────► submit job (status: queued)
                          return {job_id, status: "accepted",      NOTIFY job_events
                                  mailbox: "..."}              claim job
  ◄── accepted                                                    FOR UPDATE SKIP LOCKED
                                                                  execute subprocess
                                                                  write artifacts → NFS
                                                                  mark complete
  Agent checks mailbox:                                           NOTIFY job_events
    read /vault/mailbox/SCIMAC/site_plan/{job_id}/report.html
```

### Data flow: hosted service proxy

```
Browser                    Datum Sync                     Origin
──────                     ──────────                     ──────
GET /serve/catalogue/ ──► lookup hosted_services
                           name=catalogue
                           origin=192.168.88.102:3027

                            proxy request ─────────────────► VM102:3027
  ◄── response ◄─────────── return response
```

---

## 7. Phase Breakdown

### Phase 1: Vault Gate (next)

**Goal:** Vault access through Datum Sync's MCP endpoint, with token-scoped path enforcement.

**What to build:**

#### 5.1 Database migrations

Two columns, one migration (`migrations/007_vault_scope.sql`):

```sql
-- Boundary graph: what each agent is permitted
ALTER TABLE service_accounts ADD COLUMN vault_scope JSONB;

-- Drone delegation: job-level scope slice, intersection of account scope
-- and dispatching agent's grant. Prevents privilege escalation via drone.
ALTER TABLE jobs ADD COLUMN delegated_vault_scope JSONB;
```

Example `vault_scope` value:
```json
{
  "read": ["dev/**", "shared/long_term/**", "logs/**"],
  "write": ["dev/**"],
  "quarantine": ["quarantine/research/**"],
  "promote": ["quarantine/**", "shared/long_term/**"],
  "deny": ["secrets/**", "private/**"]
}
```

NULL `vault_scope` = no vault access. `deny` wins over allow. When `delegated_vault_scope` is set on a job, the vault gate uses it in place of (not in addition to) the account-level scope — the delegation is always a strict subset.

#### 5.1a vault_scope coherence validation (pySHACL)

Python path matching enforces that calls conform to the scope. It cannot validate that the scope itself is internally consistent. A `VaultScopeShape` in `spec/shapes/vault_scope.ttl` runs at account create/update time — not on the job hot path.

Constraints the shape encodes:
- `promote` requires `quarantine` (cannot promote what you cannot quarantine-write)
- `write` on a path implies `read` on the same path
- `deny` patterns must be disjoint from `read`/`write` (deny always wins — overlap is a config error)
- `max_tier=1` accounts cannot be granted `promote` scope

```python
# datum_sync/accounts.py — before INSERT/UPDATE
from pyshacl import validate as shacl_validate

def _check_vault_scope(scope: dict) -> None:
    graph = _scope_to_rdf(scope)   # ~30 lines, converts dict to RDF graph
    conforms, _, report = shacl_validate(graph, shacl_graph=VAULT_SCOPE_SHAPE)
    if not conforms:
        raise ValueError(f"Invalid vault_scope: {report}")
```

`VAULT_SCOPE_SHAPE` is loaded once at startup from `spec/shapes/vault_scope.ttl`. One new dependency: `pyshacl`. This is a deterministic graph constraint check — it calls no model and does no inference.

#### 5.2 New workspace type: `vault/*` (built-in, not repository-managed)

These are not read from a repository's `manifest.json`. They are built into the MCP endpoint as fixed tools, available to any account whose `vault_scope` permits them.

| MCP tool | Parameters | Auth check |
|---|---|---|
| `vault_read` | `path` (string, required), `max_chars` (int, optional) | `path` must match a `read` pattern in vault_scope |
| `vault_write` | `path` (string, required), `content` (string, required) | `path` must match a `write` pattern |
| `vault_search` | `query` (string, required), `limit` (int, optional) | Any account with vault_scope can search |
| `quarantine_write` | `path` (string, required), `content` (string, required) | `path` must match a `quarantine` pattern |
| `quarantine_promote` | `source` (string, required), `destination` (string, required) | Must have `promote` scope; source must match quarantine pattern, destination must match write pattern |
| `vault_graph_search` | `query` (string, required), `limit` (int, optional) | Any vault-scoped account |
| `vault_graph_note` | `path` (string, required) | Must have `read` scope for the path |

All calls are logged to the Datum Sync `job_log` (as workspace `vault/read`, `vault/write`, etc.).

The vault graph tools (graph search, note, backlinks, tags, stats) are the ones currently in `vault-mcp.py`. They require access to the PostgreSQL graph index — which runs on the orchestrator DB at `:5433`. This is the first cross-DB integration point.

The vault filesystem tools (`vault_read`, `vault_write`) operate directly on the NFS mount at `/vault/`. VM112 already has access.

#### 5.3 MCP endpoint changes

The built-in vault tools need to be merged into the MCP `catalogue()` alongside workspace-published tools. The simplest approach:

```python
async def catalogue(conn, principal):
    """Tool name → (repository, workspace, manifest), for this principal."""
    # Existing: workspaces from repositories
    repo_tools = await _repo_catalogue(conn, principal)

    # New: vault tools, conditionally added based on vault_scope
    vault_tools = await _vault_catalogue(principal)

    return {**repo_tools, **vault_tools}
```

No conflict is possible: vault tools are prefixed `vault_read`, `vault_write` etc. while repo tools are prefixed `{repo}__{workspace}`.

The `service_accounts.rate_limit_per_min` column already exists — vault_read calls should be rate-limited differently from vault_write calls (read is cheap, write destroys data).

#### 5.4 Path validation

A path validation module `datum_sync/vault.py`:

- Normalise paths (strip trailing slash, reject `..`)
- Match against glob patterns in `vault_scope.read`, `.write`, `.quarantine`, etc.
- `deny` patterns always win
- Return `403 FORBIDDEN` with a structured error, not `404 NOT_FOUND` (distinguish "doesn't exist" from "not allowed to see it")

#### 5.5 What does NOT change

- The existing `datum-local` MCP vault tools are still present for backward compatibility. Over time they get deprecated in favour of the Datum Sync path.
- Agents that are configured to talk to Datum Sync get the gated vault tools. Agents that aren't retain local access (current behaviour).
- The transition is opt-in: add a Datum Sync MCP server to the agent config, remove the local vault tools when ready.

#### Files to create/modify

| File | Change |
|---|---|
| `migrations/007_vault_scope.sql` | ALTER service_accounts (vault_scope) + ALTER jobs (delegated_vault_scope) |
| `spec/shapes/vault_scope.ttl` | New: SHACL shape for vault_scope coherence validation |
| `datum_sync/vault.py` | New module: path validation, glob matching, read/write/search implementations |
| `datum_sync/mcp.py` | Add vault tools to `catalogue()`, add `_vault_catalogue()` |
| `datum_sync/auth.py` | Add `vault_scope` + `delegated_vault_scope` to `Principal`; expose `allows_vault_path()` |
| `datum_sync/accounts.py` | Add `vault_scope` to account create/update; call `_check_vault_scope()` before write |
| `datum_sync/ui.py` | Show vault_scope in account detail screen |
| `requirements.txt` | Add `pyshacl` |

---

### Phase 2: Production Deployment

**Goal:** Datum Sync runs 24/7 on VM112 as a systemd service, with health checks, log rotation, and the two databases linked. This comes before onboarding — you cannot register member holons before the Head holon is persistent.

#### 2.1 Systemd unit

```
[Unit]
Description=Datum-Sync gateway
After=network-online.target docker.service

[Service]
Type=simple
User=marcus
WorkingDirectory=/home/marcus/projects/datum-sync
ExecStart=/home/marcus/projects/datum-sync/.venv/bin/python -m datum_sync.runner
Restart=always
RestartSec=5
Environment=DATABASE_URL=postgresql://datumsync:...@localhost:5435/datumsync
Environment=PUBLIC_URL=http://192.168.88.112:8200
Environment=DATUM_SYNC_AUTH=on
Environment=DATUM_SYNC_ENCRYPTION_KEY=...
Environment=DATUM_SYNC_ORCHESTRATOR_URL=postgresql://...@localhost:5433/orchestrator
```

#### 2.2 Cross-DB integration via FDW

The vault graph tools need access to the orchestrator DB's vault index tables. The cleanest approach without merging databases is a **foreign data wrapper** from Datum Sync DB to orchestrator DB:

```sql
CREATE EXTENSION postgres_fdw;
CREATE SERVER orchestrator FOREIGN DATA WRAPPER postgres_fdw
  OPTIONS (host 'localhost', port '5433', dbname 'orchestrator');
CREATE USER MAPPING FOR datumsync SERVER orchestrator
  OPTIONS (user 'datumsync', password '...');
CREATE FOREIGN TABLE vault_graph_nodes (
  ...columns from orchestrator's vault graph table...
) SERVER orchestrator OPTIONS (table_name 'actual_table_name');
```

This keeps the two databases independent (no schema coupling) while allowing Datum Sync to query the vault index for graph search and note retrieval.

#### 2.3 Agent trace correlation

When an agent calls a vault tool through Datum Sync, the MCP session already has a trace identity from the agent. The job record stores it. When we need to correlate "which agent conversation caused this vault read", we look up the trace ID in the orchestrator DB's `datum_messages` or `session_log` table via FDW.

No new infrastructure needed — just propagate the existing trace ID through the MCP request and into the `jobs` table as a `trace_id` column:

```sql
ALTER TABLE jobs ADD COLUMN trace_id TEXT;
```

The agent sends it as a JSON-RPC `meta` field or the hub generates one per session. Either way, the orchestrator and Datum Sync can share it.

#### 2.4 Log rotation, backups, health

- **Log rotation:** systemd journald handles stdout/stderr. `job_log` table prunes entries older than 90 days.
- **DB backup:** `pg_dump datumsync` to NFS `/vault/backups/datumsync/` daily via cron.
- **Health check:** `GET /health` returns `{"status": "ok", "worker": "running", "db": "ok"}`. Monitored by a simple cron script that emails Marcus on failure.

---

### Phase 3: Agent Onboarding

**Goal:** The Datum agent (and OpenClaw, Hermes, research drones) connect to Datum Sync as an MCP server and get gated vault tools + job execution. Onboarding happens after Phase 2 — there is no point registering member holons before the boundary and the Head holon are persistent.

#### 3.1 Service accounts per agent

| Agent | Token scope | Vault scope | Tier |
|---|---|---|---|
| `datum-main` | repos: `SCIMAC`, `Testing` | read: `dev/**, shared/**, logs/**`; write: `dev/**` | 3 |
| `openclaw` | repos: `SCIMAC` | read: `shared/**`; write: none | 1 |
| `hermes` | repos: none | read: `shared/long_term/**` | 1 |
| `research-drones` | repos: none | read: `shared/**, quarantine/**`; write: `quarantine/research/**`; promote: `quarantine/research/** → shared/long_term/**` | 2 |

**Drone authority model:** A research drone is a transient member holon, instantiated at dispatch and dissolved on completion. Its vault scope is a delegated slice of the dispatching agent's scope — not an independent grant. The `jobs.delegated_vault_scope` column (added in Phase 1) carries this slice. The vault gate checks it and rejects any access that exceeds it, even if the drone's service account would otherwise permit it. A drone cannot be exploited to promote beyond its parent's scope.

#### 3.2 MCP configuration

Each agent's MCP config adds Datum Sync as a server:

```json
{
  "mcpServers": {
    "datum-sync": {
      "url": "http://192.168.88.112:8200/mcp",
      "headers": {
        "Authorization": "Bearer ds_token_abc123..."
      }
    }
  }
}
```

The agent now has two MCP server sets:
- **Local** (datum-local): bash, file I/O, project tools
- **Remote** (datum-sync): vault tools, job execution, FME workspaces

The local vault tools (vault-mcp.py, datum-local's vault tools) are removed once the remote equivalents are verified stable.

---

### Phase 4: Hosted Service Proxy

**Goal:** Catalogue, Shots, Drone Monitor served through Datum Sync on a single domain. This is a projection layer — convenient but not structurally load-bearing. It comes last.

#### 4.1 Register the three PWAs as hosted_services

```sql
INSERT INTO hosted_services (name, type, repository, workspace, status)
VALUES
  ('catalogue', 'service/pwa', '_system', '_system', 'running'),
  ('shots',     'service/pwa', '_system', '_system', 'running'),
  ('drones',    'service/dashboard', '_system', '_system', 'running');
```

The existing `hosted_services` table has origin-less static services (they point at a job artifact directory). PWAs on other VMs need an `origin_url` column added so the proxy knows where to forward requests.

**Migration:** Add `origin_url` to `hosted_services`:

```sql
ALTER TABLE hosted_services ADD COLUMN origin_url TEXT;
```

When `origin_url` is set, `/serve/{name}/` reverse-proxies to that URL instead of serving from `path`.

#### 4.2 Reverse proxy

Add a catch-all route in `api.py`:

```python
@router.get("/serve/{name:str}/{subpath:path}")
async def serve_hosted(request: Request, name: str, subpath: str = ""):
    row = await services.get(conn, name)
    if row is None:
        raise ApiError(404, "NOT_FOUND", f"no such service: {name}")

    if row["origin_url"]:
        # Proxy to the origin
        url = f"{row['origin_url']}/{subpath}"
        return await _proxy_request(url, request)
    else:
        # Static file serving (existing behaviour)
        target = services.resolve(row, subpath)
        return FileResponse(target)
```

Where `_proxy_request()` uses `httpx.AsyncClient` (already a dependency in the project's `pyproject.toml` or `requirements.txt`) to stream the response.

#### 4.3 Auth gates

- **Catalogue**: public (read-only search). No auth required.
- **Shots**: public (screenshot viewer). No auth required.
- **Drone Monitor**: auth-required. Serves an OAuth login screen before proxying.

The `auth_required` column already exists in `hosted_services` (implicit in the CHECK constraint — need to verify). If not, add it:

```sql
ALTER TABLE hosted_services ADD COLUMN auth_required BOOLEAN NOT NULL DEFAULT false;
```

When `auth_required` is true, `/serve/{name}/` returns 401 with a WWW-Authenticate challenge before proxying.

#### 4.4 Drone Monitor TLS

VM111 uses a self-signed cert on port 3025. The proxy needs to either:
- Trust the self-signed cert (add it to the system CA bundle)
- Or skip TLS verification for that origin (env var `DATUM_SYNC_INSECURE_ORIGINS=drones`)

Both approaches are fine for a single-user system. The self-signed cert is acceptable as long as the proxy logs a warning on every request.

#### Files to create/modify

| File | Change |
|---|---|
| `migrations/008_hosted_origins.sql` | Add `origin_url` and `auth_required` to hosted_services |
| `datum_sync/services.py` | Add `_proxy_request()`, update `resolve()` for origin-based services |
| `datum_sync/api.py` | Add `/serve/{name}/{subpath}` route with proxy/static dispatch |
| `datum_sync/ui.py` | Show origin info and auth status in service detail screen |

---

## 8. Open Questions for Review

1. **FDW vs logical replication vs unified schema?** Phase 3 assumes FDW for cross-DB queries. Are you happy with two databases that talk via FDW, or would you prefer to merge Datum Sync into the orchestrator DB as a separate schema?

2. **vault_scope format.** I've specified JSONB with `read`, `write`, `quarantine`, `promote`, `deny` as flat glob arrays. Is this granular enough? Do we need per-repo vault scopes (e.g. "can only read vault/dev/SCIMAC/")?

3. **Quarantine promote — who approves?** Currently `quarantine_promote` is restricted to accounts with the `promote` vault_scope key. Should promote require two accounts (two-agent approval) or is single-actor fine for the single-user setup?

4. **Drone Monitor auth.** What auth mechanism for the drone proxy? OAuth PKCE (same as the web UI) or a separate bearer token? If OAuth, the Drone Monitor becomes a protected service that requires a Datum Sync session — which means the user signs in at the hub first.

5. **Syncthing or NFS for mailbox?** The mailbox (job results) could go to NFS (`/vault/mailbox/` — all VMs see it) or Syncthing (`~/shared-stanger/datum-mailbox/` — reaches Stanger Bridge). Which transport should be primary?

6. **Agent vault tool deprecation.** Once Datum Sync vault tools are stable, how quickly do we remove the local equivalents? Immediate cutover, or a grace period where both work?

7. **Pipeline (port 5274).** Vite dev server — prototype only. Do we keep it on the deferred list, deprecate it, or rebuild it as a native Datum Sync hosted service when it's ready?

---

## 9. Risks

| Risk | Mitigation |
|---|---|
| **Vault gate becomes a bottleneck** — every vault read goes through Datum Sync, adding latency | Vault graph and search queries hit the DB via FDW (fast). File reads hit NFS directly (same as local). The MCP overhead is minimal (<10ms per call). |
| **Proxy doubles network traffic** — Catalogue and Shots traffic goes VM102 → VM112 → client | Single-user. Network overhead is negligible on LAN. |
| **Agent cutover confusion** — some vault tools work, some don't, depending on MCP config | Keep local tools working in parallel. Document which are deprecated. Remove in one batch. |
| **Two-DB split makes queries harder** — finding the full picture of an incident requires both databases | FDW gives unified query capability. Trace IDs link jobs ↔ conversations. |
| **Self-signed cert on VM111 blocks proxy** | The proxy skips TLS verification for configured origins. Explicit, logged, intentional. |

---

## 10. Success Criteria

After Phase 1:
- [ ] The Datum agent calls `vault_read` through Datum Sync MCP and gets the same file it would get locally
- [ ] A token without vault scope gets 403 on vault tools
- [ ] A token with `dev/**` read scope cannot read `shared/private/**`
- [ ] Every vault access is logged in `job_log` with path, account, and timestamp

After Phase 2:
- [ ] Datum Sync runs as a systemd service, survives reboot
- [ ] Orchestrator DB vault index is queryable from Datum Sync via FDW
- [ ] Agent trace IDs propagate through job records

After Phase 3:
- [ ] All four agent types have service accounts with appropriate scopes
- [ ] A drone job's `delegated_vault_scope` blocks access outside the dispatching agent's grant
- [ ] Local vault tools are removed from datum-local MCP
- [ ] Agent config only references Datum Sync for vault access

After Phase 4:
- [ ] `http://192.168.88.112:8200/serve/catalogue/` proxies to Catalogue
- [ ] `http://192.168.88.112:8200/serve/shots/` proxies to Shots
- [ ] `http://192.168.88.112:8200/serve/drones/` requires auth before proxying