# 04 — Database Schema

PostgreSQL 16. Applied by `python -m datumgate migrate`, which records each
file in `schema_migrations(filename, applied_at, checksum)` and refuses to
run a file whose checksum changed after it was applied.

Migrations are numbered and grouped by build step (`18-build-plan.md`). The
DDL below is the **final state**; the build plan says which migration file
introduces which part. Every `CHECK` here is intentional: they are the
statements of invariants that the code relies on and that make certain
functions total.

## 000_extensions.sql

```sql
CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS pgcrypto;   -- gen_random_uuid()

CREATE TABLE schema_migrations (
    filename   TEXT PRIMARY KEY,
    checksum   TEXT NOT NULL,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

PostGIS is present because workspaces in the reference domain are
geospatial and connections to PostGIS databases are tested with it; the
gateway's own tables do not use geometry.

## 001_principals.sql

```sql
CREATE TABLE principals (
    id            SERIAL PRIMARY KEY,
    name          TEXT NOT NULL UNIQUE
                      CHECK (name ~ '^[a-z][a-z0-9._:-]{1,62}$'),
    kind          TEXT NOT NULL CHECK (kind IN ('human','agent','system')),
    parent_id     INTEGER REFERENCES principals(id) ON DELETE RESTRICT,
    "grant"       JSONB NOT NULL,
    description   TEXT,
    disabled      BOOLEAN NOT NULL DEFAULT false,
    created_by    INTEGER REFERENCES principals(id) ON DELETE SET NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at  TIMESTAMPTZ,
    -- A principal cannot be its own parent; deeper cycles are refused in code
    -- by walking the chain at write time with a depth bound.
    CHECK (parent_id IS NULL OR parent_id <> id),
    -- Tier is checked here as well as in the Pydantic model so that a row
    -- written by hand cannot carry an impossible value.
    CHECK (("grant"->>'tier')::int BETWEEN 1 AND 5)
);
CREATE INDEX principals_parent_idx ON principals (parent_id);

INSERT INTO principals (name, kind, "grant", description) VALUES
 ('system:worker', 'system',
  '{"tier":3,"repositories":["*"],"connections":{"use":["*"],"proxy":[]},
    "vault":{"read":[],"write":[],"quarantine":[],"promote":[],"deny":[]},
    "limits":{},"admin":false}',
  'The worker, when it submits scheduled and automated jobs'),
 ('system:local', 'system',
  '{"tier":5,"repositories":["*"],"connections":{"use":["*"],"proxy":[]},
    "vault":{"read":[],"write":[],"quarantine":[],"promote":[],"deny":[]},
    "limits":{},"admin":true}',
  'The CLI on the box, when run without --as');
```

## 002_credentials.sql

```sql
CREATE TABLE oauth_clients (
    client_id      TEXT PRIMARY KEY,
    client_name    TEXT,
    secret_hash    TEXT,                     -- NULL for public (PKCE) clients
    redirect_uris  TEXT[] NOT NULL CHECK (array_length(redirect_uris,1) >= 1),
    grant_types    TEXT[] NOT NULL DEFAULT ARRAY['authorization_code','refresh_token'],
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_used_at   TIMESTAMPTZ
);

CREATE TABLE credentials (
    id            BIGSERIAL PRIMARY KEY,
    principal_id  INTEGER NOT NULL REFERENCES principals(id) ON DELETE CASCADE,
    kind          TEXT NOT NULL CHECK (kind IN
                    ('token','password','session','oauth_access','oauth_refresh')),
    secret_hash   TEXT NOT NULL,
    label         TEXT,
    client_id     TEXT REFERENCES oauth_clients(client_id) ON DELETE CASCADE,
    audience      TEXT,
    scope         TEXT,
    expires_at    TIMESTAMPTZ,
    revoked_at    TIMESTAMPTZ,
    rotated_to    BIGINT REFERENCES credentials(id) ON DELETE SET NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_used_at  TIMESTAMPTZ,
    -- oauth kinds must name a client; the others must not.
    CHECK ((kind LIKE 'oauth_%') = (client_id IS NOT NULL))
);
-- Lookup path for every authenticated request.
CREATE UNIQUE INDEX credentials_hash_idx ON credentials (secret_hash);
CREATE INDEX credentials_principal_idx ON credentials (principal_id, kind);
-- At most one live password per principal.
CREATE UNIQUE INDEX credentials_one_password_idx
    ON credentials (principal_id) WHERE kind = 'password' AND revoked_at IS NULL;

CREATE TABLE oauth_codes (
    code            TEXT PRIMARY KEY,
    client_id       TEXT NOT NULL REFERENCES oauth_clients(client_id) ON DELETE CASCADE,
    principal_id    INTEGER NOT NULL REFERENCES principals(id) ON DELETE CASCADE,
    redirect_uri    TEXT NOT NULL,
    code_challenge  TEXT NOT NULL,           -- S256 only
    resource        TEXT,                    -- RFC 8707 audience carried to the token
    scope           TEXT,
    expires_at      TIMESTAMPTZ NOT NULL,
    used_at         TIMESTAMPTZ,             -- replay must be detectable, so no DELETE
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX oauth_codes_expires_idx ON oauth_codes (expires_at);

-- Multi-process rate limiting. Only written when more than one API process
-- is configured; see 03-authority.md §8.
CREATE TABLE rate_windows (
    principal_id  INTEGER NOT NULL REFERENCES principals(id) ON DELETE CASCADE,
    window_start  TIMESTAMPTZ NOT NULL,
    count         INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (principal_id, window_start)
);
```

## 003_catalogue.sql

```sql
CREATE TABLE repositories (
    id          SERIAL PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE CHECK (name ~ '^[A-Za-z][A-Za-z0-9_-]{0,63}$'),
    description TEXT,
    path        TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE workspaces (
    id                 SERIAL PRIMARY KEY,
    repository_id      INTEGER NOT NULL REFERENCES repositories(id) ON DELETE CASCADE,
    name               TEXT NOT NULL CHECK (name ~ '^[A-Za-z][A-Za-z0-9_-]{0,63}$'),
    description        TEXT,
    current_version_id INTEGER,              -- FK added below (circular)
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (repository_id, name)
);

-- Immutable. One row per successful publish whose content hash differed
-- from the current version. `manifest` is the validated, normalised form.
CREATE TABLE workspace_versions (
    id            SERIAL PRIMARY KEY,
    workspace_id  INTEGER NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    version       TEXT NOT NULL,             -- manifest.version as declared
    content_hash  TEXT NOT NULL,             -- sha256 over the directory tree, 05 §6
    manifest      JSONB NOT NULL,
    doc_text      TEXT NOT NULL,             -- MANIFEST.md verbatim, for the UI
    published_by  INTEGER REFERENCES principals(id) ON DELETE SET NULL,
    published_by_name TEXT NOT NULL,
    published_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    status        TEXT NOT NULL DEFAULT 'active'
                      CHECK (status IN ('active','superseded','withdrawn')),
    UNIQUE (workspace_id, content_hash)
);
CREATE INDEX workspace_versions_ws_idx ON workspace_versions (workspace_id, published_at DESC);

ALTER TABLE workspaces
    ADD CONSTRAINT workspaces_current_fk
    FOREIGN KEY (current_version_id) REFERENCES workspace_versions(id) ON DELETE RESTRICT;
```

## 004_jobs.sql

```sql
CREATE TABLE workers (
    id            TEXT PRIMARY KEY,           -- "{hostname}:{pid}:{random}"
    hostname      TEXT NOT NULL,
    pid           INTEGER NOT NULL,
    concurrency   INTEGER NOT NULL,
    started_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    heartbeat_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE jobs (
    id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    -- Denormalised so the row is a complete audit record after the workspace
    -- is unpublished or the repository deleted.
    repository         TEXT NOT NULL,
    workspace          TEXT NOT NULL,
    version            TEXT NOT NULL,
    version_id         INTEGER REFERENCES workspace_versions(id) ON DELETE SET NULL,
    params             JSONB NOT NULL DEFAULT '{}'::jsonb,
    status             TEXT NOT NULL DEFAULT 'queued'
                           CHECK (status IN ('queued','running','complete','failed','cancelled')),
    priority           SMALLINT NOT NULL DEFAULT 0,      -- higher first
    principal_id       INTEGER REFERENCES principals(id) ON DELETE SET NULL,
    submitted_by       TEXT NOT NULL,                     -- principal name at submit
    effective_grant    JSONB NOT NULL,                    -- frozen, 03 §5.2
    trace_id           UUID NOT NULL,
    triggered_by       TEXT CHECK (triggered_by IS NULL OR
                                   triggered_by ~ '^(schedule|automation|resubmit|mcp|rest|upload|stream|download):'),
    parent_job         UUID REFERENCES jobs(id) ON DELETE SET NULL,
    submitted_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at         TIMESTAMPTZ,
    completed_at       TIMESTAMPTZ,
    -- Lease: which worker holds it and until when. A running row whose lease
    -- has expired is an orphan; the reaper requeues or fails it. 06 §4.
    lease_worker       TEXT REFERENCES workers(id) ON DELETE SET NULL,
    lease_until        TIMESTAMPTZ,
    attempt            SMALLINT NOT NULL DEFAULT 0,
    max_attempts       SMALLINT NOT NULL DEFAULT 2,
    error              TEXT,
    artifacts          JSONB NOT NULL DEFAULT '[]'::jsonb,
    -- Set when automations have been given their chance at this job. 09 §5.
    automations_at     TIMESTAMPTZ,
    CHECK ((status = 'running') = (lease_worker IS NOT NULL))
);
CREATE INDEX jobs_queue_idx      ON jobs (priority DESC, submitted_at) WHERE status = 'queued';
CREATE INDEX jobs_running_idx    ON jobs (lease_until) WHERE status = 'running';
CREATE INDEX jobs_workspace_idx  ON jobs (repository, workspace, submitted_at DESC);
CREATE INDEX jobs_principal_idx  ON jobs (principal_id, submitted_at DESC);
CREATE INDEX jobs_trace_idx      ON jobs (trace_id);
CREATE INDEX jobs_pending_automations_idx ON jobs (completed_at)
    WHERE automations_at IS NULL AND completed_at IS NOT NULL;

CREATE TABLE job_log (
    id      BIGSERIAL PRIMARY KEY,
    job_id  UUID NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    ts      TIMESTAMPTZ NOT NULL DEFAULT now(),
    level   TEXT NOT NULL DEFAULT 'info' CHECK (level IN ('debug','info','warn','error')),
    message TEXT NOT NULL
);
CREATE INDEX job_log_job_idx ON job_log (job_id, id);

CREATE TABLE idempotency_keys (
    key          TEXT NOT NULL,
    principal_id INTEGER NOT NULL,
    job_id       UUID NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (key, principal_id)
);

CREATE TABLE uploads (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    principal_id INTEGER REFERENCES principals(id) ON DELETE SET NULL,
    filename     TEXT NOT NULL,
    size         BIGINT NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    consumed_by  UUID REFERENCES jobs(id) ON DELETE SET NULL
);
```

## 005_connections.sql

```sql
CREATE TABLE connections (
    id            SERIAL PRIMARY KEY,
    name          TEXT NOT NULL UNIQUE CHECK (name ~ '^[A-Za-z][A-Za-z0-9_.-]{0,63}$'),
    type          TEXT NOT NULL CHECK (type IN
                    ('database','http','email_smtp','email_imap','file','oauth_client','s3')),
    tier          SMALLINT NOT NULL DEFAULT 1 CHECK (tier BETWEEN 1 AND 5),
    scope         TEXT NOT NULL DEFAULT 'global' CHECK (scope IN ('global','repository','workspace')),
    scope_targets TEXT[] NOT NULL DEFAULT '{}',
    access        TEXT NOT NULL DEFAULT 'read' CHECK (access IN ('read','write')),
    description   TEXT,
    config        JSONB NOT NULL DEFAULT '{}'::jsonb,     -- readable half
    secret        BYTEA,                                  -- key_id || nonce || ct || tag
    secret_key_id SMALLINT,                               -- which DG_SECRET_KEY_n sealed it
    created_by    TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_test_at    TIMESTAMPTZ,
    last_test_ok    BOOLEAN,
    last_test_error TEXT,
    CONSTRAINT connections_scope_targets CHECK (
        (scope = 'global' AND cardinality(scope_targets) = 0)
        OR (scope <> 'global' AND cardinality(scope_targets) > 0)),
    CHECK ((secret IS NULL) = (secret_key_id IS NULL))
);
```

## 006_triggers.sql

```sql
CREATE TABLE schedules (
    id          SERIAL PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE CHECK (name ~ '^[a-z0-9][a-z0-9-]{0,62}$'),
    repository  TEXT NOT NULL,
    workspace   TEXT NOT NULL,
    params      JSONB NOT NULL DEFAULT '{}'::jsonb,
    cron        TEXT,
    interval_s  INTEGER CHECK (interval_s IS NULL OR interval_s > 0),
    timezone    TEXT NOT NULL,
    enabled     BOOLEAN NOT NULL DEFAULT true,
    owner_id    INTEGER REFERENCES principals(id) ON DELETE SET NULL,
    owner_name  TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_run    TIMESTAMPTZ,
    last_job    UUID REFERENCES jobs(id) ON DELETE SET NULL,
    last_error  TEXT,
    next_run    TIMESTAMPTZ,
    CONSTRAINT schedules_one_trigger CHECK ((cron IS NULL) <> (interval_s IS NULL))
);
CREATE INDEX schedules_due_idx ON schedules (next_run) WHERE enabled;

CREATE TABLE automations (
    id           SERIAL PRIMARY KEY,
    name         TEXT NOT NULL UNIQUE CHECK (name ~ '^[a-z0-9][a-z0-9-]{0,62}$'),
    yaml         TEXT NOT NULL,        -- verbatim
    config       JSONB NOT NULL,       -- validated parse
    enabled      BOOLEAN NOT NULL DEFAULT true,
    owner_id     INTEGER REFERENCES principals(id) ON DELETE SET NULL,
    owner_name   TEXT NOT NULL,
    owner_grant  JSONB NOT NULL,       -- frozen at save; actions run under it
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_fired   TIMESTAMPTZ,
    last_error   TEXT
);
CREATE INDEX automations_trigger_idx
    ON automations ((config -> 'trigger' ->> 'type')) WHERE enabled;

CREATE TABLE automation_runs (
    id            BIGSERIAL PRIMARY KEY,
    automation_id INTEGER NOT NULL REFERENCES automations(id) ON DELETE CASCADE,
    fired_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    trigger_kind  TEXT NOT NULL,       -- job_complete | schedule | webhook | email
    trigger_job   UUID REFERENCES jobs(id) ON DELETE SET NULL,
    trace_id      UUID NOT NULL,
    context       JSONB NOT NULL       -- the template namespace as rendered, 09 §4
);
CREATE INDEX automation_runs_recent_idx ON automation_runs (automation_id, fired_at DESC);

-- The outbox. One row per action per firing. At-least-once with a dedupe
-- key, so a crash mid-delivery retries and a retry cannot double-post.
CREATE TABLE deliveries (
    id              BIGSERIAL PRIMARY KEY,
    run_id          BIGINT NOT NULL REFERENCES automation_runs(id) ON DELETE CASCADE,
    action_index    SMALLINT NOT NULL,
    kind            TEXT NOT NULL CHECK (kind IN
                      ('run_workspace','http_request','email','vault_write')),
    payload         JSONB NOT NULL,        -- rendered action
    dedupe_key      TEXT NOT NULL UNIQUE,  -- "{automation}:{trigger_id}:{action_index}"
    status          TEXT NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending','running','done','failed','dead')),
    attempts        SMALLINT NOT NULL DEFAULT 0,
    max_attempts    SMALLINT NOT NULL DEFAULT 5,
    next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    lease_worker    TEXT REFERENCES workers(id) ON DELETE SET NULL,
    lease_until     TIMESTAMPTZ,
    result          JSONB,
    last_error      TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at    TIMESTAMPTZ
);
CREATE INDEX deliveries_due_idx ON deliveries (next_attempt_at) WHERE status IN ('pending','running');

-- Inbound webhook triggers keep a replay window.
CREATE TABLE webhook_receipts (
    automation_id INTEGER NOT NULL REFERENCES automations(id) ON DELETE CASCADE,
    nonce         TEXT NOT NULL,
    received_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (automation_id, nonce)
);
```

## 007_hosted_services.sql

```sql
CREATE TABLE hosted_services (
    id          SERIAL PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE CHECK (name ~ '^[a-z0-9][a-z0-9-]{0,62}$'),
    kind        TEXT NOT NULL CHECK (kind IN ('static','proxy')),
    type        TEXT NOT NULL,                 -- service/static|pwa|dashboard for static; service/proxy
    repository  TEXT,                          -- owner, static only
    workspace   TEXT,
    source_job  UUID REFERENCES jobs(id) ON DELETE SET NULL,
    path        TEXT,                          -- static: absolute dir inside a job's artifacts
    origin_url  TEXT,                          -- proxy: http(s)://host:port
    insecure_tls BOOLEAN NOT NULL DEFAULT false,
    visibility  TEXT NOT NULL DEFAULT 'principal'
                    CHECK (visibility IN ('public','principal','tier')),
    min_tier    SMALLINT CHECK (min_tier IS NULL OR min_tier BETWEEN 1 AND 5),
    created_by  TEXT,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK ((kind = 'static' AND path IS NOT NULL AND repository IS NOT NULL AND workspace IS NOT NULL)
        OR (kind = 'proxy'  AND origin_url IS NOT NULL)),
    CHECK ((visibility = 'tier') = (min_tier IS NOT NULL))
);
CREATE INDEX hosted_services_owner_idx ON hosted_services (repository, workspace);
```

## 008_vault.sql

```sql
-- Two-phase promotion out of quarantine. Immediate promotions still write a
-- row (status 'done') so the history is uniform.
CREATE TABLE promotions (
    id             BIGSERIAL PRIMARY KEY,
    source         TEXT NOT NULL,             -- normalised vault path
    destination    TEXT NOT NULL,
    requested_by   INTEGER REFERENCES principals(id) ON DELETE SET NULL,
    requested_name TEXT NOT NULL,
    requested_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    approved_by    INTEGER REFERENCES principals(id) ON DELETE SET NULL,
    approved_name  TEXT,
    decided_at     TIMESTAMPTZ,
    status         TEXT NOT NULL DEFAULT 'pending'
                       CHECK (status IN ('pending','done','rejected','expired')),
    content_hash   TEXT NOT NULL,             -- of the source at request time; re-checked at approval
    reason         TEXT,
    trace_id       UUID NOT NULL
);
CREATE INDEX promotions_pending_idx ON promotions (requested_at) WHERE status = 'pending';
```

## 009_audit.sql

```sql
CREATE TABLE audit_log (
    id              BIGSERIAL PRIMARY KEY,
    ts              TIMESTAMPTZ NOT NULL DEFAULT now(),
    trace_id        UUID NOT NULL,
    client_trace_id TEXT,                     -- X-Trace-Id, untrusted
    actor_id        INTEGER,                  -- no FK: rows outlive principals
    actor_name      TEXT NOT NULL,
    actor_kind      TEXT NOT NULL,
    via             TEXT NOT NULL CHECK (via IN ('rest','mcp','ui','cli','worker','serve','oauth')),
    verb            TEXT NOT NULL,            -- 14-audit.md §3 vocabulary
    target_kind     TEXT,                     -- job | workspace | vault | connection | principal | ...
    target          TEXT,                     -- safe identifier, never content
    outcome         TEXT NOT NULL CHECK (outcome IN ('ok','denied','error')),
    error_code      TEXT,
    duration_ms     INTEGER,
    governance      BOOLEAN NOT NULL DEFAULT false,
    detail          JSONB NOT NULL DEFAULT '{}'::jsonb   -- small, safe, structured
);
CREATE INDEX audit_log_ts_idx        ON audit_log (ts DESC);
CREATE INDEX audit_log_actor_idx     ON audit_log (actor_name, ts DESC);
CREATE INDEX audit_log_trace_idx     ON audit_log (trace_id);
CREATE INDEX audit_log_target_idx    ON audit_log (target_kind, target, ts DESC);
CREATE INDEX audit_log_governance_idx ON audit_log (ts DESC) WHERE governance;
CREATE INDEX audit_log_denied_idx    ON audit_log (ts DESC) WHERE outcome = 'denied';
```

## JSON codec note

asyncpg returns JSONB as text unless a codec is registered. `db.py` MUST
register `json`/`jsonb` codecs on every pool connection (`init=` hook) so
that every module receives `dict`/`list`. This removes an entire class of
"string where dict expected" defects and the per-module decode helpers that
otherwise accumulate.

```python
async def _init(conn):
    await conn.set_type_codec('jsonb', encoder=json.dumps, decoder=json.loads, schema='pg_catalog')
    await conn.set_type_codec('json',  encoder=json.dumps, decoder=json.loads, schema='pg_catalog')
```

## Later migrations (introduced by documents 21–23)

| File | Adds |
|---|---|
| `010_memory.sql` | `memory_namespaces`, `memory_entries`, `memory_history` (`21 §3.1`); conditional `pgvector` |
| `010b_federation.sql` | `connections.type` gains `'mcp'`; `pending_calls` (`21 §5.1`); `federation_catalogue_cache(connection, fetched_at, tools JSONB, resources JSONB)` |
| `011_lifecycle.sql` | `principals.state`, `restricted_from`, `review_due_at`, `last_reviewed_*`, `metadata`; `registration_codes`; `claim_codes` (`22`) |
| `012_teams.sql` | `teams`, `team_id` on principals/connections/repositories/hosted_services; `audit_rollups`; `review_signals` (`23`) |
