-- 001_core.sql
-- Core schema: repositories, workspaces, jobs, job log, service accounts,
-- idempotency. Covers build steps 1-4 (schema, workspace loader, job engine,
-- REST API). Connections, schedules, automations, OAuth and hosted services
-- land in later migrations alongside the code that uses them.

CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS pgcrypto;   -- gen_random_uuid()


-- Repositories -------------------------------------------------------------

CREATE TABLE repositories (
    id          SERIAL PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    description TEXT,
    path        TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);


-- Workspaces ---------------------------------------------------------------
-- A workspace row exists only once it has passed the publish gate. The
-- manifest is stored verbatim so a run does not depend on re-reading disk,
-- and so an on-disk edit cannot silently change a published interface.

CREATE TABLE workspaces (
    id            SERIAL PRIMARY KEY,
    repository_id INTEGER NOT NULL REFERENCES repositories(id) ON DELETE CASCADE,
    name          TEXT NOT NULL,
    description   TEXT,
    version       TEXT,
    manifest      JSONB NOT NULL,
    published_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    published_by  TEXT,
    UNIQUE (repository_id, name)
);


-- Service accounts ---------------------------------------------------------
-- token_hash is sha256(token) hex. The raw token is never persisted.
-- repo_scope NULL means all repositories.

CREATE TABLE service_accounts (
    id                SERIAL PRIMARY KEY,
    name              TEXT NOT NULL UNIQUE,
    description       TEXT,
    token_hash        TEXT UNIQUE,
    token_expires     TIMESTAMPTZ,
    max_tier          INTEGER NOT NULL DEFAULT 1
                          CHECK (max_tier BETWEEN 1 AND 4),
    repo_scope        TEXT[],
    connection_grants TEXT[],
    rate_limit_per_min INTEGER,
    disabled          BOOLEAN NOT NULL DEFAULT false,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_used_at      TIMESTAMPTZ
);

CREATE INDEX service_accounts_token_hash_idx ON service_accounts (token_hash);


-- Jobs ---------------------------------------------------------------------
-- repository/workspace are denormalised text, not FKs: a job record is an
-- audit trail and must survive its workspace being unpublished or deleted.

CREATE TABLE jobs (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    repository   TEXT NOT NULL,
    workspace    TEXT NOT NULL,
    params       JSONB NOT NULL DEFAULT '{}'::jsonb,
    status       TEXT NOT NULL DEFAULT 'queued'
                     CHECK (status IN ('queued','running','complete','failed','cancelled')),
    submitted_by TEXT,
    submitted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at   TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    error        TEXT,
    artifacts    JSONB NOT NULL DEFAULT '[]'::jsonb,
    parent_job   UUID REFERENCES jobs(id) ON DELETE SET NULL   -- set on resubmit
);

CREATE INDEX jobs_status_idx       ON jobs (status);
CREATE INDEX jobs_submitted_at_idx ON jobs (submitted_at DESC);
CREATE INDEX jobs_workspace_idx    ON jobs (repository, workspace);

-- Partial index for the queue poll: the hot path only ever reads queued rows.
CREATE INDEX jobs_queue_idx ON jobs (submitted_at) WHERE status = 'queued';


-- Job log ------------------------------------------------------------------

CREATE TABLE job_log (
    id      BIGSERIAL PRIMARY KEY,
    job_id  UUID NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    ts      TIMESTAMPTZ NOT NULL DEFAULT now(),
    level   TEXT NOT NULL DEFAULT 'info'
                CHECK (level IN ('debug','info','warn','error')),
    message TEXT NOT NULL
);

CREATE INDEX job_log_job_id_idx ON job_log (job_id, id);


-- Idempotency --------------------------------------------------------------
-- Maps an Idempotency-Key (scoped to the submitting account) to the job it
-- created, so a retried submission returns the original job instead of
-- queueing a duplicate.

CREATE TABLE idempotency_keys (
    key        TEXT NOT NULL,
    account    TEXT NOT NULL,
    job_id     UUID NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (key, account)
);
