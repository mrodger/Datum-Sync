# 06 — Schema: migrations 016 → 023

Each file is one transaction (`migrate.py` wraps it), checksummed, and
covered by `tests/test_schema_drift.py`. Every table carries the comment
block explaining why it exists — the existing migrations set that standard
and a worker must keep it (the comment text below is the minimum). Numbers
are relative to `015_drop_stale_cols.sql`. Down migrations are not written;
each file states its revert path.

## 016_principals.sql — kind, tree, state, derived admin

```sql
ALTER TABLE service_accounts
    ADD COLUMN kind       TEXT NOT NULL DEFAULT 'human'
                          CHECK (kind IN ('human','agent','system')),
    ADD COLUMN parent_id  INTEGER NULL REFERENCES service_accounts(id) ON DELETE RESTRICT,
    ADD COLUMN state      TEXT NOT NULL DEFAULT 'active'
                          CHECK (state IN ('pending','active','restricted','disabled','retired','rejected')),
    ADD COLUMN proxy_grants     TEXT[] NOT NULL DEFAULT '{}',
    ADD COLUMN limits           JSONB  NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN federation_scope JSONB  NULL,
    ADD COLUMN metadata         JSONB  NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN restricted_from  JSONB  NULL,
    ADD COLUMN restricted_reason TEXT  NULL,
    ADD COLUMN review_due_at    TIMESTAMPTZ NULL,
    ADD COLUMN last_reviewed_at TIMESTAMPTZ NULL,
    ADD COLUMN last_reviewed_by TEXT NULL,
    ADD COLUMN created_by       TEXT NULL;

-- Existing rows: disabled=true becomes state='disabled'. `disabled` is kept
-- for one release as the column the thirty existing readers use; it is
-- re-derived by trigger so the two cannot disagree. Dropped in 018.
UPDATE service_accounts SET state = 'disabled' WHERE disabled;

CREATE FUNCTION service_accounts_sync_disabled() RETURNS trigger AS $$
BEGIN
  NEW.disabled := NEW.state NOT IN ('active','restricted');
  NEW.is_admin := NEW.max_tier >= 4;
  RETURN NEW;
END $$ LANGUAGE plpgsql;
CREATE TRIGGER service_accounts_sync BEFORE INSERT OR UPDATE ON service_accounts
  FOR EACH ROW EXECUTE FUNCTION service_accounts_sync_disabled();

-- is_admin is derived from tier from here on (01 §4.11). Rewrite once so the
-- constraint can be added; any account that was admin below tier 4 is
-- promoted to tier 4 rather than demoted -- widening an existing operator's
-- tier is visible in the UI; silently removing admin from one is not.
UPDATE service_accounts SET max_tier = 4 WHERE is_admin AND max_tier < 4;
UPDATE service_accounts SET is_admin = (max_tier >= 4);
ALTER TABLE service_accounts ADD CONSTRAINT service_accounts_admin_is_tier
    CHECK (is_admin = (max_tier >= 4));

CREATE INDEX service_accounts_parent_idx ON service_accounts (parent_id);
CREATE INDEX service_accounts_state_idx  ON service_accounts (state) WHERE state <> 'active';
CREATE INDEX service_accounts_review_idx ON service_accounts (review_due_at) WHERE review_due_at IS NOT NULL;
```

Revert: drop the trigger, function, constraint and columns; `disabled` was
never dropped.

The ancestor walk used by the resolver, for reference (not a migration
object):

```sql
WITH RECURSIVE chain AS (
  SELECT a.*, 0 AS depth FROM service_accounts a WHERE a.id = $1
  UNION ALL
  SELECT p.*, chain.depth + 1 FROM service_accounts p JOIN chain ON p.id = chain.parent_id
  WHERE chain.depth < 8
) SELECT * FROM chain ORDER BY depth;
```

## 017_fold_agents.sql — agents become principals

```sql
-- One row per agent, under its account, inheriting the account's scope so
-- that nothing narrows on the day this runs (03 §1.1).
INSERT INTO service_accounts
    (name, description, kind, parent_id, state, max_tier, repo_scope, vault_scope,
     proxy_grants, rate_limit_per_min, is_admin, created_at, last_used_at, created_by)
SELECT ag.name, 'migrated from agents', 'agent', ag.account_id,
       CASE WHEN ag.disabled OR sa.disabled THEN 'disabled' ELSE 'active' END,
       sa.max_tier, sa.repo_scope, sa.vault_scope, ag.proxy_grants, sa.rate_limit_per_min,
       false, ag.created_at, ag.last_used_at, sa.name
  FROM agents ag JOIN service_accounts sa ON sa.id = ag.account_id;

ALTER TABLE account_tokens ADD COLUMN max_tier INTEGER NULL CHECK (max_tier BETWEEN 1 AND 5);

INSERT INTO account_tokens (account_id, label, token_hash, created_at, last_used_at)
SELECT p.id, 'agent', ag.token_hash, ag.created_at, ag.last_used_at
  FROM agents ag JOIN service_accounts p ON p.name = ag.name AND p.kind = 'agent'
 WHERE ag.token_hash IS NOT NULL;

-- Not dropped here. auth.resolve stops reading `agents` in the same change;
-- the table stays for one release as the revert path (as 012 did for
-- service_accounts.token_hash). Dropped in 018.
```

Note the trigger from 016 sets `is_admin=false` for the agent rows even if a
parent is tier 4: the INSERT's `max_tier` is copied, so the trigger will set
`is_admin = (max_tier >= 4)`. An agent under a tier-4 account therefore
*inherits tier 4*. That is the pre-migration behaviour (an agent token had
`is_admin=False` but the parent's tier), now made explicit and visible in
the Principals screen so an operator narrows it. `09 WP1` ships a CLI report
listing every agent at tier ≥ 3 for exactly this reason.

## 018_drop_agents_disabled.sql — one release later

```sql
DROP TABLE agents;                       -- proxy_log keeps agent_name as text
ALTER TABLE service_accounts DROP COLUMN disabled;
-- trigger function updated to stop assigning NEW.disabled
ALTER TABLE jobs DROP COLUMN delegated_vault_scope;   -- never read (01 §4.4)
```

## 019_registration.sql — enrolment

```sql
CREATE TABLE registration_codes (
    id           SERIAL PRIMARY KEY,
    code_hash    TEXT NOT NULL UNIQUE,            -- sha256; raw shown once
    created_by   INTEGER NOT NULL REFERENCES service_accounts(id),
    parent_id    INTEGER NOT NULL REFERENCES service_accounts(id),  -- the sponsor
    template     JSONB NOT NULL,                  -- authority tuple the registrant may narrow
    auto_approve BOOLEAN NOT NULL DEFAULT false,
    max_uses     INTEGER NOT NULL DEFAULT 1 CHECK (max_uses >= 1),
    uses         INTEGER NOT NULL DEFAULT 0,
    expires_at   TIMESTAMPTZ NOT NULL,
    revoked_at   TIMESTAMPTZ,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (uses <= max_uses)
);

CREATE TABLE registration_claims (
    id           SERIAL PRIMARY KEY,
    principal_id INTEGER NOT NULL REFERENCES service_accounts(id) ON DELETE CASCADE,
    claim_hash   TEXT NOT NULL UNIQUE,
    code_id      INTEGER NOT NULL REFERENCES registration_codes(id),
    expires_at   TIMESTAMPTZ NOT NULL,            -- 24 h from approval (or from enrol when auto)
    claimed_at   TIMESTAMPTZ,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

## 020_sessions.sql

```sql
CREATE TABLE mcp_sessions (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    principal_id     INTEGER NOT NULL REFERENCES service_accounts(id) ON DELETE CASCADE,
    credential_kind  TEXT NOT NULL,               -- token | oauth
    token_id         INTEGER NULL,                -- account_tokens.id or oauth_tokens.id, by kind
    client_name      TEXT, client_version TEXT, protocol_version TEXT,
    started_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    ended_at         TIMESTAMPTZ,
    end_reason       TEXT CHECK (end_reason IN ('client','idle','revoked','limit','restart'))
);
CREATE INDEX mcp_sessions_live_idx ON mcp_sessions (principal_id, last_seen_at) WHERE ended_at IS NULL;

ALTER TABLE audit_log ADD COLUMN session_id UUID NULL;
CREATE INDEX audit_log_session_idx ON audit_log (session_id) WHERE session_id IS NOT NULL;

ALTER TABLE jobs ADD COLUMN grant_snapshot JSONB NULL;   -- 03 §7; NULL for pre-migration rows
```

## 021_device_flow_and_outcomes.sql

```sql
CREATE TABLE oauth_device_codes (
    id               SERIAL PRIMARY KEY,
    device_code_hash TEXT NOT NULL UNIQUE,
    user_code        TEXT NOT NULL,               -- 8 chars, unambiguous alphabet; unique among undecided
    client_id        TEXT NOT NULL REFERENCES oauth_clients(client_id) ON DELETE CASCADE,
    principal_id     INTEGER NOT NULL REFERENCES service_accounts(id) ON DELETE CASCADE,
    scope            TEXT NOT NULL,
    resource         TEXT NOT NULL,
    interval_seconds INTEGER NOT NULL DEFAULT 5,
    requested_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at       TIMESTAMPTZ NOT NULL,
    last_polled_at   TIMESTAMPTZ,
    decision         TEXT CHECK (decision IN ('approved','denied')),
    approved_scope   TEXT,                        -- may be narrower than scope
    decided_by       INTEGER, decided_name TEXT, decided_at TIMESTAMPTZ,
    consumed_at      TIMESTAMPTZ
);
CREATE UNIQUE INDEX oauth_device_codes_user_code_idx ON oauth_device_codes (user_code) WHERE decision IS NULL;

-- 'denied' becomes writable: federation guards are the first writer that can
-- distinguish a refusal from a failure (05 §4.2). mcp_call_log is not widened;
-- its disagreement with audit_log from here on is the recorded reason it retires.
ALTER TABLE audit_log DROP CONSTRAINT IF EXISTS audit_log_outcome_check;   -- inline CHECK in 011, Postgres default name
ALTER TABLE audit_log ADD CONSTRAINT audit_log_outcome_check CHECK (outcome IN ('ok','error','denied'));
```

## 022_federation.sql

```sql
-- connections.type gains 'mcp'. The CHECK from 005 is inline and auto-named
-- by Postgres (`connections_type_check`, as 010 did for the tier check --
-- verify against pg_constraint before relying on the name). The existing
-- six values are kept exactly.
ALTER TABLE connections DROP CONSTRAINT IF EXISTS connections_type_check;
ALTER TABLE connections ADD CONSTRAINT connections_type_check
    CHECK (type IN ('database','http','email_smtp','email_imap','file','oauth_client','mcp'));
ALTER TABLE connections ADD COLUMN federation_status JSONB NULL;

CREATE TABLE federated_tools (
    connection    TEXT NOT NULL REFERENCES connections(name) ON DELETE CASCADE,
    upstream_name TEXT NOT NULL,
    tool_name     TEXT NOT NULL UNIQUE,
    description   TEXT,
    input_schema  JSONB NOT NULL,
    annotations   JSONB,
    guarded       BOOLEAN NOT NULL DEFAULT false,
    fetched_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (connection, upstream_name)
);

CREATE TABLE federated_resources (
    connection    TEXT NOT NULL REFERENCES connections(name) ON DELETE CASCADE,
    upstream_uri  TEXT NOT NULL,
    name TEXT, description TEXT, mime_type TEXT, is_template BOOLEAN NOT NULL DEFAULT false,
    fetched_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (connection, upstream_uri)
);

CREATE TABLE pending_calls (
    id              BIGSERIAL PRIMARY KEY,
    connection      TEXT NOT NULL, tool TEXT NOT NULL,
    args            JSONB NOT NULL,                -- stored on purpose: an approver must see what they approve
    label           TEXT NOT NULL,
    requested_by    INTEGER NOT NULL, requested_name TEXT NOT NULL,
    requested_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at      TIMESTAMPTZ NOT NULL,
    session_id      UUID, trace_id UUID NOT NULL,
    grant_snapshot  JSONB NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending','approved','rejected','expired','executed','failed')),
    decided_by      INTEGER, decided_name TEXT, decided_at TIMESTAMPTZ,
    result          JSONB
);
CREATE INDEX pending_calls_open_idx ON pending_calls (requested_at) WHERE status = 'pending';
```

## 023_retire_mirrors.sql — after verification (09 WP7)

```sql
DROP TABLE mcp_call_log;
DROP TABLE proxy_log;
```

Preceded by a recorded check (a script under `tools/`) that for the last N
days every `mcp_call_log` row has an `audit_log` row with the same actor,
verb and minute, and the count difference is explained by `denied` rows.
The two UI screens that read the old tables are re-pointed in the same
work package.

## Policy values (config.py, environment)

| Name | Default |
|---|---|
| `POLICY_BASELINE_TIER` | 2 |
| `POLICY_BASELINE_SESSIONS` | 1 |
| `POLICY_ELEVATED_SESSIONS` | 4 |
| `POLICY_MAX_DELEGATION_DEPTH` | 4 |
| `POLICY_REVIEW_INTERVAL_DAYS` | 90 |
| `POLICY_INACTIVITY_RESTRICT_DAYS` | 30 |
| `POLICY_PENDING_TTL_DAYS` | 7 |
| `POLICY_CLAIM_TTL_HOURS` | 24 |
| `POLICY_ELEVATION_AUTO_APPROVE_SCOPES` | `mcp` |
| `POLICY_PENDING_CALL_TTL_HOURS` | 72 |
| `SESSION_IDLE_SECONDS` (MCP) | 900 |
| `FEDERATION_REFRESH_SECONDS` (default per connection) | 300 |
| `FEDERATION_LIST_TIMEOUT_SECONDS` | 5 |
| `FEDERATION_CALL_MAX_BYTES` | 1048576 |
| `RETENTION_UNUSED_OAUTH_CLIENT_DAYS` | 30 |
| `DEFAULT_JOBS_PER_HOUR` / `DEFAULT_CONCURRENT_JOBS` | 60 / 2 |

Read once at import like the rest of `config.py`; no `policy.yaml` (`11 D-10`).
