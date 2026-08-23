# Datum-Sync — Component Specifications

## 1. Repositories

Workspaces are organised into repositories. A repository is a directory on disk with a
corresponding record in the database.

**On disk:**
```
repositories/
  SCIMAC/
    site_plan/
      main.py
      manifest.json
      MANIFEST.md
    core_log/
      main.py
      manifest.json
      MANIFEST.md
```

**DB record:**
```sql
CREATE TABLE repositories (
    id          SERIAL PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    description TEXT,
    path        TEXT NOT NULL,
    created_at  TIMESTAMPTZ DEFAULT now()
);
```

**Publish gate** (all must pass before a workspace is callable):
1. `manifest.json` validates against schema
2. All declared connections exist and are accessible at the required tier
3. `MANIFEST.md` is present and non-empty
4. Optional: smoke test (`main.py run --smoke`) exits 0

---

## 2. Services

Four service types, each a distinct URL pattern and response behaviour:

| Service | Path | Behaviour |
|---|---|---|
| Job Submitter | `POST /rest/v1/transformations/submit/{repo}/{ws}` | Async — returns 202 + Location |
| Data Streaming | `GET /stream/{repo}/{ws}` | Sync — returns body directly, SSE for progress |
| Data Download | `GET /download/{repo}/{ws}` | Sync — returns `Content-Disposition: attachment` |
| Data Upload | `POST /upload/{repo}/{ws}` | Multipart file intake, passes to workspace |

---

## 3. Published Parameters

Declared in `manifest.json`. Typed, validated, grouped.

**Types:** `STRING`, `INTEGER`, `FLOAT`, `BOOLEAN`, `FILE`, `LOOKUP_CHOICE`

**Example:**
```json
{
  "name": "JOB_ID",
  "type": "STRING",
  "required": true,
  "description": "Stratum job number",
  "group": "Input"
}
```

```json
{
  "name": "OUTPUT_FORMAT",
  "type": "LOOKUP_CHOICE",
  "default": "HTML",
  "choices": ["HTML", "PDF", "JSON"],
  "group": "Output"
}
```

---

## 4. Connections

Scoped credential store. Credentials are encrypted at rest and never passed directly to
workspace code — workspaces receive resolved connection objects.

**Three-dimensional model:**

| Dimension | Values | Notes |
|---|---|---|
| Sensitivity tier | 1–4 | Service account must have `max_tier >= connection.tier` to use |
| Scope | `global`, `repository`, `workspace` | Controls which workspaces can access the connection |
| Access | `read`, `write` | Declared in manifest; enforced at publish gate |

**Connection record:**
```json
{
  "name": "StratumDB_read",
  "type": "database",
  "tier": 2,
  "scope": "repository",
  "scope_targets": ["SCIMAC"],
  "access": "read",
  "description": "Stratum PostgreSQL — read only"
}
```

**Connection types:** `database`, `http`, `email_smtp`, `email_imap`, `file`, `oauth_client`

---

## 5. Job Engine

Async job execution with durable SSE streaming.

**Stack:** asyncpg + pg_notify + asyncio worker pool

**Job lifecycle:**
```
submitted → queued → running → complete | failed | cancelled
```

**Workspace contract:**
```python
async def run(params: dict, emit, connections: dict) -> list[dict]:
    await emit("progress", {"pct": 0.0, "message": "Starting"})
    # ... do work ...
    await emit("progress", {"pct": 1.0, "message": "Done"})
    return [
        {"name": "report", "type": "text/html", "content": "<html>..."},
        {"name": "site_plan", "type": "image/jpeg", "path": "/tmp/plan.jpg"},
    ]
```

**DB tables:**
```sql
CREATE TABLE jobs (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    repository      TEXT NOT NULL,
    workspace       TEXT NOT NULL,
    params          JSONB,
    status          TEXT NOT NULL DEFAULT 'queued',
    submitted_by    TEXT,           -- service account name
    submitted_at    TIMESTAMPTZ DEFAULT now(),
    started_at      TIMESTAMPTZ,
    completed_at    TIMESTAMPTZ,
    error           TEXT,
    artifacts       JSONB           -- list of output artifact descriptors
);

CREATE TABLE job_log (
    id        BIGSERIAL PRIMARY KEY,
    job_id    UUID REFERENCES jobs(id),
    ts        TIMESTAMPTZ DEFAULT now(),
    level     TEXT,
    message   TEXT
);
```

pg_notify channel: `job_events` — payload `{"job_id": "...", "status": "...", "pct": 0.5}`

---

## 6. Schedules

APScheduler-backed cron and interval triggers.

```sql
CREATE TABLE schedules (
    id          SERIAL PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    repository  TEXT NOT NULL,
    workspace   TEXT NOT NULL,
    params      JSONB,
    cron        TEXT,               -- cron expression, null if interval
    interval_s  INTEGER,            -- seconds, null if cron
    timezone    TEXT DEFAULT 'Pacific/Auckland',
    enabled     BOOLEAN DEFAULT true,
    last_run    TIMESTAMPTZ,
    next_run    TIMESTAMPTZ
);
```

---

## 7. Automations

YAML-configured triggers and action chains. No visual builder — YAML editor with schema
validation in the web UI.

**Trigger types:** `schedule`, `job_complete`, `webhook`, `email`

**Action types:** `run_workspace`, `deliver`, `http_request`

**Example:**
```yaml
name: site-plan-email-delivery
enabled: true
trigger:
  type: job_complete
  workspace: site_plan
  status: success
actions:
  - type: deliver
    channel: email
    to: [andrew@example.com, david@example.com]
    artifacts: [site_plan_pdf]
    subject: "Site plan ready — {{params.JOB_ID}}"
  - type: http_request
    url: "{{SLACK_WEBHOOK}}"
    body: '{"text": "Site plan delivered for job {{params.JOB_ID}}"}'
```

---

## 8. Resources

Managed file store. Mounted read-only into workspace runs via the `connections` dict.

```
resources/
  shared/
    templates/
    reference_data/
  SCIMAC/
    logos/
    soil_types.csv
```

REST API: `GET|PUT|DELETE /rest/v1/resources/{mount}/{path}`

---

## 9. REST API

**Prefix:** `/rest/v1/` for programmatic API. Service paths at root (no prefix).

**Endpoint families:**

```
# Repositories and workspaces
GET    /rest/v1/repositories
POST   /rest/v1/repositories
GET    /rest/v1/repositories/{repo}
GET    /rest/v1/repositories/{repo}/workspaces
GET    /rest/v1/repositories/{repo}/workspaces/{ws}

# Job submission and management
POST   /rest/v1/transformations/submit/{repo}/{ws}   → 202 + Location header
GET    /rest/v1/transformations/jobs/id/{id}
DELETE /rest/v1/transformations/jobs/id/{id}          → cancel
POST   /rest/v1/transformations/jobs/id/{id}/resubmit
GET    /rest/v1/transformations/jobs/id/{id}/log

# Service paths (user-facing, no prefix)
GET    /stream/{repo}/{ws}
GET    /download/{repo}/{ws}
POST   /upload/{repo}/{ws}

# Schedules
GET|POST        /rest/v1/schedules
GET|PATCH|DELETE /rest/v1/schedules/{id}

# Automations
GET|POST        /rest/v1/automations
GET|PATCH|DELETE /rest/v1/automations/{id}

# Connections
GET|POST        /rest/v1/connections
GET|PATCH|DELETE /rest/v1/connections/{name}
POST            /rest/v1/connections/{name}/test

# Resources
GET|PUT|DELETE  /rest/v1/resources/{mount}/{path}

# Security
GET|POST        /rest/v1/security/accounts
GET|PATCH|DELETE /rest/v1/security/accounts/{id}
POST            /rest/v1/security/accounts/{id}/rotate

# Services (hosted)
GET|POST        /rest/v1/services
GET|PATCH|DELETE /rest/v1/services/{name}
POST            /rest/v1/services/{name}/start
POST            /rest/v1/services/{name}/stop
POST            /rest/v1/services/{name}/restart

# System
GET  /rest/v1/engines
GET  /health
GET  /openapi.json
GET  /docs
```

**Standard additions over naive REST:**
- `Idempotency-Key` header on job submission — safe to retry
- Consistent error envelope: `{"status": 400, "code": "INVALID_PARAMETER", "message": "...", "detail": {}}`
- Per-service-account rate limiting
- Bulk job submission: `POST /rest/v1/transformations/submit-bulk`

---

## 10. Tokens / Auth

**Two auth paths:**

| Caller | Method |
|---|---|
| Scripts, REST API, automation | Bearer token (service account) |
| MCP clients (Claude.ai, ChatGPT, Copilot) | OAuth 2.0 PKCE |

**Bearer tokens:**
- Opaque 32-byte random, stored as `sha256(token)` — raw value never persisted
- Optional TTL; `null` = non-expiring
- Scopes inherit from service account (max_tier, repo scope, connection grants)
- Rotation: `POST /rest/v1/security/accounts/{id}/rotate` — atomic swap

**OAuth 2.0 (MCP clients):**

Library: `authlib` on FastAPI.

Endpoints:
```
GET  /.well-known/oauth-authorization-server   ← MCP client discovery
GET  /oauth/authorize                           ← consent screen
POST /oauth/token                               ← code exchange + PKCE verify
POST /oauth/revoke                              ← token revocation
```

An OAuth grant mints a scoped service account token. No separate permission model.

**Service account record:**
```sql
CREATE TABLE service_accounts (
    id              SERIAL PRIMARY KEY,
    name            TEXT NOT NULL UNIQUE,
    token_hash      TEXT,
    token_expires   TIMESTAMPTZ,
    max_tier        INTEGER DEFAULT 1,
    repo_scope      TEXT[],         -- null = all repos
    connection_grants TEXT[],       -- explicit connection name grants
    created_at      TIMESTAMPTZ DEFAULT now(),
    last_used_at    TIMESTAMPTZ
);
```

---

## 11. Web UI

Single-page app, vanilla JS. Talks exclusively to `/rest/v1/`.

**Three sections:**

| Section | Contents |
|---|---|
| **Workspaces** | Repository browser, workspace runner (published parameters form), recent jobs per workspace |
| **Jobs** | Queue view, log stream, cancel, resubmit |
| **Admin** | Connections, service accounts, OAuth grants, schedules, automations (YAML editor), hosted services |

Design system: match Datum-UI conventions (Navy `#1D3A5C`, amber `#C89632`, dark `#0F1923`,
DM Sans body, Space Grotesk headings, JetBrains Mono code, Phosphor icons).

---

## 12. Hosted Services

Workspaces can return `service/*` artifacts to publish a persistent hosted service.

**Service types:**

| Type | Runtime | Supervision |
|---|---|---|
| `service/static` | datum-sync serves files directly | None |
| `service/pwa` | Static + manifest registration | None |
| `service/dashboard` | Static, calls `/rest/v1/` for data | None |
| `service/interactive` | Supervised subprocess, fixed port | systemd unit, health check, restart |
| `service/notebook` | Supervised subprocess, websocket-aware | systemd unit, health check, restart |

**Workspace artifact examples:**
```python
{"name": "app", "type": "service/static",      "path": "/path/to/dist/"}
{"name": "app", "type": "service/pwa",         "path": "/path/to/dist/", "manifest": "manifest.json"}
{"name": "app", "type": "service/dashboard",   "path": "/path/to/dist/"}
{"name": "app", "type": "service/interactive", "command": "python serve.py", "port": 8400}
{"name": "app", "type": "service/notebook",    "command": "jupyter ...",    "port": 8888}
```

**URL:** All service types accessible at `/serve/{name}/` — proxy layer handles the difference.

**DB table:**
```sql
CREATE TABLE hosted_services (
    id          SERIAL PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    type        TEXT NOT NULL,              -- service/* type
    source_job  UUID REFERENCES jobs(id),
    path        TEXT,                       -- for static types
    command     TEXT,                       -- for supervised types
    port        INTEGER,                    -- for supervised types
    status      TEXT DEFAULT 'stopped',     -- stopped | starting | running | error
    pid         INTEGER,
    started_at  TIMESTAMPTZ,
    last_health TIMESTAMPTZ
);
```
