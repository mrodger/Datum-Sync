# Datum-Sync — Revised Phase 3 Proposal

> **Status:** Updated following codebase audit (Sep 2026)  
> **Key changes from v1:** SHACL → Python scope validation; FDW → connection-based search; drone orchestration is a separate subsystem; hosted service proxy uses a `kind` column; Phase 1 vault gate is substantially complete.

---

## 1. The Vision in One Sentence

Datum Sync is the **deterministic infrastructure layer** that every agent (Datum, OpenClaw, Hermes) connects to for vault access, job execution, tool discovery, credential management, and hosted service proxying. It contains no model, calls no LLM, and evaluates no agent intent. The intelligence is in the agents. The gateway enforces rules, runs scripts, and logs everything.

---

## 2. Principles

1. **Datum Sync contains no LLM and calls no model.** It runs jobs (subprocesses: FME, Python, shell), validates parameters against declared schemas, enforces auth via bearer tokens and glob-matched vault scopes, and logs everything. No prompt, no inference, no reasoning. Governance is structural: rate limits, scope enforcement, audit logs. Agents reason; the hub enforces.
2. **Agents keep local autonomy.** Short-lived file reads, code execution, shell — all handled locally. The hub gates production data and destructive operations.
3. **MCP is the primary agent-facing protocol.** Discovery (`tools/list`), submission (`tools/call`) and result delivery all happen through MCP Streamable HTTP. No custom client needed for any agent type.
4. **Jobs are fire-and-forget.** The MCP response returns a `job_id` and `status: accepted`. Results land in the vault's mailbox directory. Agents check for results when they need them.
5. **One token per agent, scoped to its tier.** The hub holds the credential model. Agents carry bearer tokens with path scopes, repo scopes, and proxy grants. No shared keys.
6. **The proxy layer unifies the PWAs.** Catalogue, Shots, Drone Monitor, and future apps live behind Datum Sync on a single domain with auth where needed.
7. **Drone orchestration is a separate subsystem.** Research drones are managed by a control plane on VM111, not as native Datum Sync principals. Drones receive delegated scope via that subsystem and interact with Datum Sync as transient MCP clients — they are not registered as `service_accounts`. This keeps the audit log clean (drone traffic is ephemeral) and avoids polluting the account registry with hundreds of short-lived entries.

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

---

## 4. Holonic Architecture

Datum Sync is a **Head holon** in a Moderated Group holarchy (Koestler/W3C Holon CG framing). This is descriptive, not prescriptive — the code does not change.

| Holon component | Datum Sync implementation |
|---|---|
| Knowledge graph | `repositories` + `workspaces` + `workspaces.manifest` — the tool catalogue |
| Context/event graph | `jobs` + `job_log` + `automation_runs` — execution history |
| Boundary graph | `service_accounts.vault_scope` — what each agent is permitted |
| Projections | `mcp.py → catalogue()` — per-principal filtered `tools/list` output |

Each agent VM is a **Member holon**: full local autonomy for operations that don't cross the boundary. A `service_accounts` row is the membership record. Authority flows down: a delegated vault scope (for transient clients) is a strict subset of the delegating principal's scope — it cannot exceed its parent's grant.

Full analysis: `spec/holonic-shacl-analysis.md`.

---

## 5. Current State

### What exists

| Component | Status | Notes |
|---|---|---|
| **Datum Sync codebase** | 26+ modules, 13 migrations (incl. B1-B5 tranche), production-quality MCP endpoint | Deployed to VM112 at `:8200` |
| **Datum Sync DB** | Separate PostGIS instance on `:5435` | 142 jobs, 5,492 audit events, 6 workspaces, 3 auth principals |
| **Vault gate (Phase 1)** | Substantially complete | `vault_read`, `vault_write`, `vault_list` built-in MCP tools with glob-scoped enforcement, deny-wins, traversal protection. Ships in `datum_sync/vault.py`, auth wiring in `auth.py`, CLI/UI support. |
| **OAuth 2.0 PKCE** | Implemented | Login, tokens, sessions, 5 auth tiers |
| **Agents** | `agents.py` — sub-identities with proxy grants, labelled tokens, disable/enable | 13/13 registry entries |
| **Credential proxy** | `proxy.py` — SSRF guard, auth injection, two-level identity | Audit logged |
| **Schedules + automations** | Cron/interval schedules, job-completion triggers, loop protection | `schedules.py`, `automations.py` |
| **Audit log** | Trace ID per request, 5,492 events and counting | `audit_log` table |
| **Hosted services** | Static + dashboard types; atomic swap on re-publish | `services.py`, `/serve/{name}/` |
| **Secrets rotation** | Multi-key scheme (`_01`, `_02`, `CURRENT`) | `crypto.py`, migration B4 |
| **Multi-token accounts** | Labelled tokens per account, revocable by label | Migration 012 |
| **Workspace CRUD** | Routes, manifest validation, MCP tools `tools/list` + `tools/call` | `api.py`, `manifest.py` |
| **Orchestrator DB** | PostGIS 16 on `:5433` | 841 convos, 30k messages, 47k brain files |
| **Vault** | 3.4TB NFS share mounted on VM102, VM111, VM112 | `/vault/dev/`, `/vault/shared/`, `/vault/quarantine/`, `/vault/_inbox/` |
| **Catalogue** | PWA on VM102:3027 | Search over vault + tools index |
| **Shots** | PWA on VM102:8330 | Session screenshot viewer with service worker |
| **Drone Monitor** | HTTPS on VM111:3025 | Self-signed cert. Currently an MCP tool, not a standalone UI. |
| **Pipeline** | Vite dev on VM102:5274 | Prototype, deferred |

### What doesn't exist yet

- `quarantine_write` and `quarantine_promote` built-in MCP tools
- `vault_search` built-in tool (scoped search via pluggable backend)
- Hosted service proxy for PWAs on other VMs (`kind='proxy'`)
- Production deployment (systemd, health checks, backups)
- Agent MCP connections (no agent currently calls Datum Sync's MCP endpoint)
- Drone orchestration subsystem (separate project)
- Claude.ai connector registration

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
  (via VM111 ctrl)   ──MCP──►  └──────┬───────┘  └──────┬────────┘   │
                           │         │                  │           │
                           │  ┌──────┴───────┐  ┌──────┴────────┐  │
                           │  │  Vault Gate   │  │  Hosted       │  │
                           │  │  /mcp tools:  │  │  Services     │  │
                           │  │  vault_read   │  │  Proxy        │  │
                           │  │  vault_write  │  │               │  │
                           │  │  vault_list   │  │  catalogue    │  │
                           │  │  vault_search │  │  shots        │  │
                           │  │  quarantine   │  │  drones       │  │
                           │  │  promote      │  │               │  │
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
                           kind='proxy', origin=192.168.88.102:3027

                            proxy request ─────────────────► VM102:3027
  ◄── response ◄─────────── return response
```

---

## 7. Phase Breakdown

### Phase 1: Vault Gate — Substantially Complete ✅

The vault gate shipped across commits `8ff664b` → `9b85f3d`. It is fully built and tested.

**Shipped components:**

| Component | What it does |
|---|---|
| Migration `007_vault_scope.sql` | Adds `vault_scope` JSONB to `service_accounts`, `delegated_vault_scope` to `jobs` |
| `datum_sync/vault.py` | Path normalisation (strip trailing slash, reject `..`), glob matching, deny-wins, `validate_scope()` coherence check |
| `datum_sync/auth.py` | `Principal` has `vault_scope`, `allows_vault_path()` method |
| `datum_sync/mcp.py` | `vault_read`, `vault_write`, `vault_list` built-in MCP tools, conditionally added via `_vault_catalogue()` when `principal.vault_scope` is non-null |
| `datum_sync/accounts.py` | `--vault-scope` CLI arg, `validate_scope()` called at create/update time |
| `datum_sync/ui.py` | Vault scope shown in account detail screen |
| 36 tests | Scope validation, glob matching, deny-wins, traversal protection |

**Scope validation** (`vault.validate_scope()`) is pure Python — no SHACL, no RDF, no external dependency. It checks four structural rules at account write time:

1. `promote` requires a `quarantine` pattern (cannot promote what you cannot quarantine-write)
2. `write` on a path implies `read` on the same path
3. `deny` patterns must not overlap with `read`/`write` (deny always wins — overlap is a config error)
4. `max_tier=1` accounts cannot be granted `promote` scope

This approach was preferred over pySHACL for three reasons: no runtime dependency, faster (no RDF translation), and the constraints are simple enough that a dedicated graph validation engine adds complexity without benefit. The SHACL shape file at `spec/shapes/vault_scope.ttl` remains as documentation but does not run in production.

#### Remaining Phase 1 work — quarantine tools

Two built-in MCP tools not yet implemented:

| Tool | Parameters | Auth check |
|---|---|---|
| `quarantine_write` | `path` (string, required), `content` (string, required) | `path` must match a `quarantine` pattern in vault_scope; writes with `.origin.json` sidecar marking untrusted content |
| `quarantine_promote` | `source` (string, required), `destination` (string, required) | Must have `promote` scope; source must match `quarantine` pattern, destination must match `write` pattern; promotes from quarantine to trusted vault with audit trail |

The sidecar mechanism and promote logic are specified in `spec/datum-gate/` but not coded.

#### Vault search — deferred to later phase

`vault_search` is not a built-in tool for Phase 1. Instead of hardcoding it (which would couple Datum Sync to an index it doesn't own), search is implemented as a **connection-backed** tool: agents register a search backend as an HTTP connection, and the search tool proxies the query with scope re-filtering. This keeps the vault gate focused on file I/O and scope enforcement, not indexing. See Phase 3 below.

---

### Phase 2: Production Deployment

**Goal:** Datum Sync runs 24/7 on VM112 as a systemd service, with health checks, log rotation, and backups.

#### 2.1 Systemd unit

```
[Unit]
Description=Datum-Sync gateway
After=network-online.target

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
```

#### 2.2 Log rotation, backups, health

- **Log rotation:** systemd journald handles stdout/stderr. `job_log` table prunes entries older than 90 days.
- **DB backup:** `pg_dump datumsync` to NFS `/vault/backups/datumsync/` daily via cron.
- **Health check:** `GET /health` returns `{"status": "ok", "worker": "running", "db": "ok"}`. Monitored by a simple cron script that emails Marcus on failure.

#### 2.3 Agent trace correlation

When an agent calls a vault tool through Datum Sync, the MCP session carries a trace identity. The `audit_log` table already stores trace IDs. Correlation between "which agent conversation caused this vault read" is done by looking up the trace ID in the orchestrator DB's `datum_messages` or `session_log` table — no FDW needed because this is a human investigation step, not a time-critical query.

---

### Phase 3: Vault Search & Agent Onboarding

**Goal:** Agents connect to Datum Sync as an MCP server and get gated vault tools + job execution. Vault search becomes available through a connection-backed search tool.

#### 3.1 Vault search via pluggable backend

`vault_search` uses a connection store entry rather than a hardcoded PostgreSQL FDW. The tool:

1. Reads the principal's search backend from connections (e.g. an HTTP endpoint on VM111 that indexes vault content)
2. Forwards the query to that backend
3. Re-filters results against the principal's `vault_scope` — any result whose path falls outside the principal's `read` scope is dropped
4. Returns only what the principal is allowed to see

This means:
- No FDW coupling between Datum Sync DB and orchestrator DB
- The search backend can be replaced (e.g. switch from BM25 to vector search without touching Datum Sync)
- The scope filter is always enforced by Datum Sync, not delegated to the search index

#### 3.2 Service accounts per agent

Research drones are not registered in this table — they have a separate orchestration subsystem.

| Agent | Token scope | Vault scope | Tier |
|---|---|---|---|
| `datum-main` | repos: `SCIMAC`, `Testing` | read: `dev/**, shared/**, logs/**`; write: `dev/**` | 3 |
| `openclaw` | repos: `SCIMAC` | read: `shared/**`; write: none | 1 |
| `hermes` | repos: none | read: `shared/long_term/**` | 1 |

**Transient clients (research drones):** Drone vault access flows through `jobs.delegated_vault_scope` — a strict subset of the dispatching principal's scope. The drone never has a `service_accounts` row. It authenticates with a short-lived token minted by the drone orchestration subsystem on VM111, and the vault gate checks the delegated scope on the job row. Access exceeding that scope is rejected regardless of what the drone's token claims.

#### 3.3 MCP configuration

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

The local vault tools (vault-mcp.py, datum-local's vault tools) are deprecated with a two-week grace period once the remote equivalents are verified stable. During the grace period, both work; after it, the local tools are removed.

---

### Phase 4: Hosted Service Proxy

**Goal:** Catalogue, Shots, Drone Monitor served through Datum Sync on a single domain. This is a projection layer — convenient but not structurally load-bearing. It comes last.

#### 4.1 Schema — `kind` column, not `origin_url`

The existing `hosted_services` table has `CHECK (hosted_services_runnable)` requiring `path` for static services. Adding origin-based proxying requires a `kind` column that branches the constraint:

```sql
ALTER TABLE hosted_services ADD COLUMN kind TEXT NOT NULL DEFAULT 'static'
  CHECK (kind IN ('static', 'proxy'));

ALTER TABLE hosted_services ADD COLUMN origin_url TEXT;

-- Relax existing CHECK to account for proxy kind
ALTER TABLE hosted_services DROP CONSTRAINT hosted_services_runnable;
ALTER TABLE hosted_services ADD CONSTRAINT hosted_services_runnable CHECK (
  (kind = 'static' AND path IS NOT NULL)
  OR (kind = 'proxy' AND origin_url IS NOT NULL)
);
```

Why `kind` over bare `origin_url`? A nullable column with a conditional constraint is self-documenting — a reader sees `kind='proxy'` and immediately knows this service routes to an external origin, rather than needing to infer that `path=NULL` + `origin_url!=NULL` means proxy. The spec schema (`04-schema.md`) already uses this pattern.

#### 4.2 Register the three PWAs

```sql
INSERT INTO hosted_services (name, kind, origin_url, auth_required, repository, workspace, status)
VALUES
  ('catalogue', 'proxy', 'http://192.168.88.102:3027', false, '_system', '_system', 'running'),
  ('shots',     'proxy', 'http://192.168.88.102:8330', false, '_system', '_system', 'running'),
  ('drones',    'proxy', 'https://192.168.88.111:3025', true,  '_system', '_system', 'running');
```

The `auth_required` column already exists in the table. When true, `/serve/{name}/` returns 401 with a `WWW-Authenticate` challenge before proxying.

#### 4.3 Reverse proxy

Add a catch-all route in `api.py`:

```python
@router.get("/serve/{name:str}/{subpath:path}")
async def serve_hosted(request: Request, name: str, subpath: str = ""):
    row = await services.get(conn, name)
    if row is None:
        raise ApiError(404, "NOT_FOUND", f"no such service: {name}")

    if row["kind"] == "proxy":
        url = f"{row['origin_url']}/{subpath}"
        return await _proxy_request(url, request)
    else:
        # Static file serving (existing behaviour)
        target = services.resolve(row, subpath)
        return FileResponse(target)
```

Where `_proxy_request()` uses `httpx.AsyncClient` (already in the project's dependencies) to stream the response.

#### 4.4 Drone Monitor TLS

VM111 uses a self-signed cert on port 3025. The proxy handles this via an environment variable:

```
DATUM_SYNC_INSECURE_ORIGINS=drones
```

When the origin hostname matches an entry in `DATUM_SYNC_INSECURE_ORIGINS`, the proxy skips TLS verification and logs a warning on every request. This is explicit, intentional, and temporary — if the Drone Monitor ever becomes production-facing, it gets a proper cert.

#### 4.5 Migration file

| File | Change |
|---|---|
| `migrations/008_hosted_kind.sql` | Add `kind` and `origin_url` to `hosted_services`, replace CHECK constraint |

---

### Deferred Items (no timeline)

These are described in the spec but explicitly not planned for this phase:

| Item | Why deferred | Spec reference |
|---|---|---|
| **Claude.ai connector registration** | Requires Anthropic marketplace integration — external dependency, no clear timeline | N/A |
| **Drone orchestration subsystem** | Separate control plane on VM111, not part of Datum Sync | `spec/orchestrator/drone-control.md` |
| **Pipeline (VM102:5274)** | Vite dev prototype, no production deployment | Deferred; deprecate if untouched 6 months |
| **Per-agent tool visibility filtering** | MCP tool list is hardcoded; per-agent filtering requires tool registry | Future phase |

---

## 8. Risks

| Risk | Mitigation |
|---|---|
| **Vault gate becomes a bottleneck** — every vault read goes through Datum Sync, adding latency | File reads hit NFS directly (same as local VM). MCP overhead is minimal (<10ms per call). |
| **Proxy doubles network traffic** — Catalogue and Shots traffic goes VM102 → VM112 → client | Single-user. Network overhead is negligible on LAN. |
| **Agent cutover confusion** — some vault tools work, some don't, depending on MCP config | Two-week grace period where both work. Document which are deprecated. Remove in one batch. |
| **Self-signed cert on VM111 blocks proxy** | `DATUM_SYNC_INSECURE_ORIGINS` env var. Explicit, logged, intentional. |
| **Vault search results leak scope** — search index returns paths the agent shouldn't see | Results are re-filtered through `vault_scope` after the search backend returns them. The backend is untrusted. |

---

## 9. Success Criteria

After Phase 1 completion (quarantine tools):
- [ ] The Datum agent calls `vault_read` through Datum Sync MCP and gets the same file it would get locally
- [ ] A token without vault scope gets 403 on vault tools
- [ ] A token with `dev/**` read scope cannot read `shared/private/**`
- [ ] Every vault access is logged in `audit_log` with path, account, and timestamp
- [ ] `quarantine_write` creates a `.origin.json` sidecar on every write
- [ ] `quarantine_promote` fails if the source pattern doesn't match a quarantine path

After Phase 2:
- [ ] Datum Sync runs as a systemd service, survives reboot
- [ ] Agent trace IDs propagate through audit log entries

After Phase 3:
- [ ] All three agent types have service accounts with appropriate scopes
- [ ] A transient drone's `delegated_vault_scope` blocks access outside the dispatching agent's grant
- [ ] `vault_search` returns only paths the principal's `vault_scope` permits
- [ ] Local vault tools are deprecated with two-week grace period

After Phase 4:
- [ ] `http://192.168.88.112:8200/serve/catalogue/` proxies to Catalogue
- [ ] `http://192.168.88.112:8200/serve/shots/` proxies to Shots
- [ ] `http://192.168.88.112:8200/serve/drones/` requires auth before proxying
- [ ] Self-signed cert warning is logged, not silently swallowed