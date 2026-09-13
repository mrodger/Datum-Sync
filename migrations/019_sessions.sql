-- 019_sessions.sql
--
-- MCP sessions, the frozen grant on a job, and a session id on audit rows.
-- Per spec/agent-auth-plane/03 §5, §7 and 06 §020.
--
-- A session exists for exactly two purposes: COUNTING (limits.concurrent_
-- sessions -- "one session at a time" is a statement about a client
-- connection, not a request) and AUDIT GROUPING. No tool state lives in it;
-- every request is still fully authenticated by its bearer token.
--
-- `token_id` with `credential_kind` names the credential row the session was
-- opened with. It is what makes supersession safe (spec 12 §2): a client that
-- reconnects on the SAME credential replaces its own oldest session rather
-- than colliding with it, while a second credential still collides -- which
-- is the case the limit exists for.
--
-- Idle sessions are counted as ended when `last_seen_at` is stale; the daily
-- tick stamps `ended_at`/`idle` afterwards for the record.

CREATE TABLE mcp_sessions (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    principal_id     INTEGER NOT NULL REFERENCES service_accounts(id) ON DELETE CASCADE,
    credential_kind  TEXT NOT NULL CHECK (credential_kind IN ('token', 'oauth', 'session', 'local', 'auth-disabled')),
    token_id         BIGINT NULL,             -- account_tokens.id or oauth_tokens.id, by kind
    client_name      TEXT,
    client_version   TEXT,
    protocol_version TEXT,
    started_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    ended_at         TIMESTAMPTZ,
    end_reason       TEXT CHECK (end_reason IN ('client', 'idle', 'revoked', 'limit',
                                               'restart', 'superseded'))
);

CREATE INDEX mcp_sessions_live_idx ON mcp_sessions (principal_id, last_seen_at)
    WHERE ended_at IS NULL;

ALTER TABLE audit_log ADD COLUMN session_id UUID NULL;
CREATE INDEX audit_log_session_idx ON audit_log (session_id) WHERE session_id IS NOT NULL;

-- The authority a job was submitted under (spec 03 §7): the caller's
-- effective tuple plus its name, kind, parent, session and trace. The worker
-- resolves the job's connections against the snapshot's tier rather than the
-- live row, so narrowing a principal after submit does not change a run
-- already in flight, and widening one does not either. NULL for jobs the
-- scheduler and the automation engine submit and for every job before this
-- migration: those resolve as before, against the publish gate alone.
ALTER TABLE jobs ADD COLUMN grant_snapshot JSONB NULL;
