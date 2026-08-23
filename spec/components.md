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
- Per-service-account rate limiting — *not yet built*, and a resource quota when
  it is. Not to be confused with the consent-screen login limit under Tokens
  below, which exists and guards a guessable secret rather than a share of
  capacity.
- Bulk job submission: `POST /rest/v1/transformations/submit-bulk`

---

## 10. Tokens / Auth

**Two auth paths:**

| Caller | Method |
|---|---|
| Scripts, REST API, automation | Bearer token (service account) |
| MCP clients (Claude.ai, ChatGPT, Copilot) | OAuth 2.0 PKCE |

Both resolve to the same `Principal`, because an OAuth grant is *bound to a
service account* rather than carrying permissions of its own. `max_tier`,
`repo_scope` and `connection_grants` therefore have exactly one home.

**Bearer tokens:**
- Opaque 32-byte random, stored as `sha256(token)` — raw value never persisted
- Optional TTL; `null` = non-expiring
- Scopes inherit from service account (max_tier, repo scope, connection grants)
- Rotation: `python -m datum_sync.accounts token <name>` — replaces the hash,
  the old value stops working. There is deliberately no HTTP route that creates
  an account: the first one has nobody to authenticate it, so a bootstrap
  endpoint would be either open or seeded with a secret that has to be
  delivered somehow. A shell on the box is already the trust boundary.

sha256 and not argon2: these are 32 random bytes, so there is no dictionary to
attack, and the hash sits on the lookup path of every request. Passwords are the
opposite case and use argon2.

**OAuth 2.1 + PKCE (MCP clients):**

Hand-rolled, not `authlib`. What is actually needed is one grant type with one
client type, and the parts that matter — audience binding, refresh rotation with
reuse detection, exact redirect-URI matching — are the parts a library makes
harder to see rather than easier.

```
GET  /.well-known/oauth-protected-resource      ← RFC 9728, what a 401 points at
GET  /.well-known/oauth-authorization-server    ← RFC 8414, AS metadata
POST /oauth/register                            ← RFC 7591 dynamic registration
GET  /oauth/authorize                           ← consent screen
POST /oauth/authorize                           ← consent screen posts back
POST /oauth/token                               ← code exchange + refresh rotation
POST /oauth/revoke                              ← RFC 7009
```

Required by Claude.ai and easy to leave out:
- **A 401 must carry `WWW-Authenticate: Bearer resource_metadata="..."`.** A
  client that has never seen this server has nothing else to go on.
- **Dynamic registration is unauthenticated** and has to be: the client has no
  id before it first reaches us. What that gets an attacker is a client id,
  which grants nothing on its own — every token still requires a human to sign
  in at the consent screen.
- **RFC 8707 audience binding.** Tokens are minted for `PUBLIC_URL + /mcp` and
  refused anywhere else, so a compromised downstream server cannot replay its
  tokens here. `PUBLIC_URL` is the audience *and* the issuer, so the server
  **refuses to start without it** rather than defaulting to a localhost guess:
  a wrong value serves discovery happily and mints tokens no client can use,
  and the failure then surfaces at the client as an opaque authorization error
  with nothing pointing back at the setting. The resolved value is logged at
  startup for the same reason — set-but-wrong fails exactly like correct until
  a client tries to use a token. Note `.env` loses to an exported variable of
  the same name.
- **Refresh rotation with reuse detection** (OAuth 2.1 §4.14.2). A credential
  presented twice means someone else has a copy, so the whole grant family is
  revoked — not just the second presentation refused, because the first one
  already produced a working token.

The consent screen is the only place a *guessable* secret is accepted, so it is
the only place rate-limited: five failures for a given account name inside 15
minutes returns 429 with `Retry-After`. Three things about that limit are
deliberate.

- It is keyed on the **submitted account name, not the client address**. Behind
  a tunnel or reverse proxy every request arrives from one address, so an
  address-keyed limit either locks out all clients at once or does nothing —
  and trusting `X-Forwarded-For` instead would let the caller choose its own key
  and opt out entirely.
- It is checked **before the database is consulted**, so a locked-out name that
  does not exist behaves identically to one that does. A limiter that only
  counted real accounts would answer 429 for a name that exists and 401 for one
  that does not, turning a defence into account enumeration.
- It lives in `auth.authenticate_password` rather than in the route, so it
  cannot be left off a second caller.

Separately, a semaphore bounds *concurrent* argon2 verifications. Argon2 is
deliberately expensive and an unknown account still pays for a full hash (so the
form does not leak which names exist), which makes the login a cheap way to burn
CPU. The aim there is that the rest of the server stays responsive, not that any
login is refused — so it is a concurrency bound, not a second rate limit.

Bearer tokens are not rate-limited. They are 32 random bytes; there is no
dictionary, so a lockout would add a denial-of-service without removing an
attack that exists.

OAuth endpoints return RFC 6749 §5.2 errors (`{"error": "invalid_grant", ...}`)
rather than the project's error envelope. One envelope everywhere is the rule;
a wire format we do not own is the exception, and it stops at that module.

**MCP endpoint:** `POST /mcp`, Streamable HTTP, protocol `2025-06-18`, stateless
(no SSE channel, no session id). Each published workspace becomes one tool named
`<repo>__<workspace>`, with an input schema derived from its manifest
parameters. Two deliberate mappings:
- A workspace appears only if it publishes **`data_streaming`** — `tools/call`
  runs it and returns the output, which is exactly what that service means.
- A workspace with a **required FILE parameter is hidden.** An MCP client cannot
  perform the upload that produces an upload id, so the tool could only ever
  fail; advertising it trades a missing tool for one the model keeps retrying.

A workspace that fails returns `isError: true` with the log, not a JSON-RPC
error — the model has to see it to react to it.

**Service account record:** see `migrations/001_core.sql` and `002_auth.sql`.
`002` adds `password_hash` (argon2, consent screen only), `is_admin`, and the
`oauth_clients` / `oauth_codes` / `oauth_tokens` tables.

**Verification.** `tests/break_the_guard.py` removes each of the 13 security
checks in turn and requires the test named for it to fail. A test asserting 403
passes just as well against a route that is broken for an unrelated reason, and
a gate that has never been seen to fail is not a gate.

---

## 11. Web UI

Single-page app, vanilla JS. Talks exclusively to `/rest/v1/`.

**Layout follows FME Flow's GUI structure exactly.** Datum visual style applied throughout —
do not invent new layout patterns; if Flow has a screen for it, mirror its structure.

### Chrome

- **Left sidebar** — collapsible, icon + label nav. Items match Flow's left nav:
  Repositories, Jobs, Schedules, Automations, Connections, Resources, Services, Admin
- **Top bar** — product name, active persona/account badge, notifications bell
- **Main content area** — context-dependent per nav item

### Screens (mirror Flow)

| Screen | Flow equivalent | Contents |
|---|---|---|
| Repositories | Repositories | Repository list → workspace cards grid |
| Workspace detail | Workspace detail | Description, published parameters form, Run button, recent jobs |
| Jobs | Jobs | Queue table: status badge, workspace, submitted by, duration, actions |
| Job detail | Job detail | Log stream (SSE), artifact download links, resubmit / cancel |
| Schedules | Schedules | Table with enable/disable toggle, next run, last run, edit |
| Automations | Automations | Table with enable/disable toggle; YAML editor on detail view |
| Connections | Connections | Table with type icon, tier badge, scope, test button |
| Resources | Resources | File browser tree |
| Services | (no Flow equiv.) | Hosted service list with type badge, status indicator, URL |
| Admin | Security | Service accounts table, OAuth grants, token rotation |

### Visual style

| Token | Value |
|---|---|
| Primary | Navy `#1D3A5C` |
| Accent | Amber `#C89632` |
| Background | Dark `#0F1923` |
| Body font | DM Sans |
| Heading font | Space Grotesk |
| Mono font | JetBrains Mono |
| Icons | Phosphor (Bold weight) |

Status badges follow Flow's colour convention mapped to Datum palette:
- Running → amber
- Complete → green
- Failed → red
- Queued → muted grey

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
