-- 009_mcp_call_log.sql
--
-- Uniform audit spine for every JSON-RPC request handled by the MCP endpoint.
-- One row per request (all methods: initialize, ping, tools/list, tools/call).
--
-- Design notes:
--
-- Identity comes from auth — account_id/name are trustworthy. There is no FK
-- on account_id because the row should survive account deletion (audit trail
-- must outlive the principal that generated it).
--
-- `target` is a SAFE summary of what was targeted: a vault path, a
-- "connection:METHOD:/path" string for proxy calls, or NULL for methods that
-- have no target (tools/list, initialize, ping). Content is never stored —
-- bodies may carry PII or secrets. Same policy as proxy_log.
--
-- `is_governance` is set server-side based on a declared path classification.
-- It flags writes to governance-sensitive paths (SOUL.md, skills/**, hooks/**)
-- so they can be queried without a path scan. An agent cannot manipulate this
-- flag by choice of path — the server classifies after normalisation.
--
-- `client_trace_id` comes from the X-Trace-Id request header. It is UNTRUSTED:
-- the client may forge, omit, or reuse it. Store it as a monitoring convenience
-- only — never use it for authorisation decisions or deduplication. Column name
-- encodes this: "client" signals origin, not server-assigned identity.
--
-- `error_code` is the JSON-RPC error code on failure (e.g. -32600), NULL on
-- success. `outcome` is always set: 'ok' or 'error'.

CREATE TABLE mcp_call_log (
    id              BIGSERIAL PRIMARY KEY,

    -- Principal identity (from auth, trustworthy).
    -- No FK — rows must survive account deletion.
    account_id      INTEGER NOT NULL,
    account_name    TEXT NOT NULL,

    -- What was called.
    method          TEXT NOT NULL,      -- tools/call | tools/list | initialize | ping
    tool_name       TEXT,               -- NULL except method = tools/call

    -- Safe target summary. Never stores content/body.
    --   vault ops  → normalised vault path (e.g. "dev/notes/foo.md")
    --   proxy      → "connection:METHOD:/path" (e.g. "openai:POST:/v1/chat/completions")
    --   others     → NULL
    target          TEXT,

    -- Server-side governance classification. True when target matches a
    -- declared governance path (SOUL.md, skills/**, hooks/**).
    is_governance   BOOLEAN NOT NULL DEFAULT false,

    -- Outcome.
    outcome         TEXT NOT NULL CHECK (outcome IN ('ok', 'error')),
    error_code      INTEGER,            -- JSON-RPC error code; NULL on success

    -- Wall-clock duration of the handler (not including auth or JSON parsing).
    duration_ms     INTEGER,

    -- Untrusted grouping hint from X-Trace-Id request header.
    -- Monitoring convenience only — never authoritative.
    client_trace_id TEXT,

    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Time-ordered spine — primary query pattern for dashboards and monitors.
CREATE INDEX mcp_call_log_created_idx ON mcp_call_log (created_at DESC);

-- Per-account activity view — "what has this account been doing".
CREATE INDEX mcp_call_log_account_idx ON mcp_call_log (account_name, created_at DESC);

-- Governance fast lane — filter to flagged rows without a full table scan.
CREATE INDEX mcp_call_log_governance_idx ON mcp_call_log (created_at DESC)
    WHERE is_governance;
