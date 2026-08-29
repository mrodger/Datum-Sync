-- 008_agents_and_proxy_log.sql
--
-- Two-level identity: service_accounts are OAuth principals (humans or
-- machines); agents are named sub-identities each with their own bearer token
-- and proxy grants. The separation exists because a single user may own
-- multiple agents, each needing different keys to different services.
--
-- proxy_grants is deliberately separate from service_accounts.connection_grants.
-- connection_grants controls which connections a workspace can resolve at job
-- time (publisher-controlled). proxy_grants controls which connections an agent
-- can forward through at call time (admin-assigned per agent). A new agent
-- gets zero grants — proxy is opt-in, no inherited access.

CREATE TABLE agents (
    id              SERIAL PRIMARY KEY,
    account_id      INTEGER NOT NULL REFERENCES service_accounts ON DELETE CASCADE,
    name            TEXT NOT NULL UNIQUE,
    token_hash      TEXT UNIQUE,
    proxy_grants    TEXT[] NOT NULL DEFAULT '{}',
    disabled        BOOLEAN NOT NULL DEFAULT false,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_used_at    TIMESTAMPTZ
);

CREATE INDEX agents_account_idx ON agents (account_id);

-- Audit trail for credential proxy calls. One row per proxy_request.
-- Bodies deliberately not stored: they may contain PII (user messages in a
-- chat completion) or secrets (the response may echo credentials). The audit
-- answers "who used which connection, when, and did it succeed".
-- No FK on agent_id — survives agent deletion.

CREATE TABLE proxy_log (
    id              BIGSERIAL PRIMARY KEY,
    agent_id        INTEGER NOT NULL,
    agent_name      TEXT NOT NULL,
    account_name    TEXT NOT NULL,
    connection_name TEXT NOT NULL,
    method          TEXT NOT NULL,
    path            TEXT NOT NULL,
    upstream_status INTEGER,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX proxy_log_created_idx ON proxy_log (created_at DESC);
CREATE INDEX proxy_log_agent_idx ON proxy_log (agent_name, created_at DESC);
