-- 011_audit_log.sql
--
-- One append-only spine for authorised actions, joined by a trace id the
-- SERVER mints. Per spec/datum-gate/14-audit.md §1.
--
-- Why this table exists
-- ---------------------
-- We already log. The problem is that the logs cannot be joined to each other.
-- A single MCP `proxy_request` tool call writes one `mcp_call_log` row and one
-- `proxy_log` row, in two tables, sharing nothing but a timestamp and an
-- account name. "Which upstream did this tool call touch" is not answerable
-- today, and under any concurrency the timestamp is not an answer either.
--
-- `mcp_call_log.client_trace_id` looks like it should solve this and does not.
-- It is the `X-Trace-Id` request header: supplied by the caller, therefore
-- neither unique nor trustworthy. An agent can send one value on every call
-- (merging unrelated work into one trace) or send another agent's value
-- (splicing itself into someone else's). `proxy_log` has no trace column at
-- all, so even a trustworthy client value would not join these two rows.
--
-- `trace_id` here is minted at request entry from uuid4 and carried down the
-- call. A client cannot forge one because a client never supplies one. The
-- header value is still kept, in `client_trace_id`, because it is useful for
-- lining our records up against a caller's own -- but it is never the join key.
-- Both columns exist on purpose: the name of each says where it came from.
--
-- Dual-write, not replacement
-- ---------------------------
-- `job_log`, `proxy_log`, `mcp_call_log` and `automation_runs` all stay, and
-- keep being written exactly as before. This table is written alongside them.
-- Retiring them is a later migration, after this one has been shown to answer
-- what they answer. Dropping them in the same change that adds this one would
-- mean the only copy of the audit trail is the copy that has never been read.
--
-- Because phase one mirrors the existing writers rather than improving on
-- them, `audit_log` and `mcp_call_log` should agree row for row -- and that
-- agreement is the check. Two known classification defects are therefore
-- reproduced here deliberately rather than fixed:
--
--   * `outcome` is ('ok','error') only, with no 'denied', because nothing in
--     the codebase can currently produce a denial that is distinguishable from
--     a success. A tool refused by `check_proxy_access` raises ApiError, which
--     `_proxy_call` catches and returns as an `isError` result -- a *successful*
--     JSON-RPC response. The existing writer records it as 'ok'. Permitting a
--     'denied' value that nothing writes would advertise a distinction the data
--     does not carry; adding it when the classification is fixed is one line.
--   * `duration_ms` on the proxy row is NULL. The proxy writer has never
--     measured itself, and inventing a number here would be worse than absent.
--
-- What is never stored: request or response bodies, vault content, secrets,
-- parameter values. `target` is an identifier only. Same policy as proxy_log
-- and mcp_call_log, and the reason both of those columns are called `target`.

CREATE TABLE audit_log (
    id              BIGSERIAL PRIMARY KEY,

    -- The join key. Server-minted per inbound request; every row that request
    -- produces carries the same value. Not null, because a row that cannot be
    -- joined is the thing this table was built to stop existing.
    trace_id        UUID NOT NULL,

    -- X-Trace-Id as sent. UNTRUSTED: forgeable, omittable, reusable.
    -- Never an authorisation input, never a dedupe key, never a join key.
    client_trace_id TEXT,

    -- The principal. Name and kind are copied rather than referenced so the
    -- row outlives the principal that made it -- same reason mcp_call_log and
    -- proxy_log carry no FK on their identity columns.
    actor_id        INTEGER NOT NULL,
    actor_name      TEXT NOT NULL,
    actor_kind      TEXT NOT NULL CHECK (actor_kind IN ('account', 'agent')),

    -- Which surface the request arrived on. Only 'mcp' is written today; the
    -- REST API and UI do not yet write audit rows. The column is here because
    -- the discriminator that makes one table work is `verb`, and `via` is what
    -- stops two surfaces' verbs colliding when the others are wired up.
    via             TEXT NOT NULL,

    -- The discriminator. `{kind}.{action}`, vocabulary in 14-audit.md §3.
    -- Written today: mcp.initialize, mcp.ping, mcp.tools.list, mcp.tools.call,
    -- proxy.request. One tools/call that reaches the proxy writes BOTH
    -- mcp.tools.call and proxy.request, under one trace -- that pair is the
    -- whole point of the table.
    verb            TEXT NOT NULL,

    -- A safe identifier, never content: a vault path, a connection name, a
    -- tool name, "connection:METHOD:/path".
    target_kind     TEXT,
    target          TEXT,

    outcome         TEXT NOT NULL CHECK (outcome IN ('ok', 'error')),
    error_code      INTEGER,        -- JSON-RPC code, or upstream HTTP status
    duration_ms     INTEGER,        -- handler time; NULL where unmeasured

    -- Server-side classification, same rule as mcp_call_log.is_governance:
    -- set after path normalisation, so a caller cannot clear it by choosing a
    -- path. Named `governance` to match the spec column.
    governance      BOOLEAN NOT NULL DEFAULT false,

    -- Small JSON: status codes, counts, field NAMES. Never values.
    -- Bounded by the writer, not by the column -- see audit.py.
    detail          JSONB,

    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- The join. This is the query the table exists for: every row of one request.
CREATE INDEX audit_log_trace_idx ON audit_log (trace_id);

-- Time-ordered spine, for dashboards and monitors.
CREATE INDEX audit_log_created_idx ON audit_log (created_at DESC);

-- Per-actor activity.
CREATE INDEX audit_log_actor_idx ON audit_log (actor_name, created_at DESC);

-- Governance fast lane, without a full scan.
CREATE INDEX audit_log_governance_idx ON audit_log (created_at DESC)
    WHERE governance;
