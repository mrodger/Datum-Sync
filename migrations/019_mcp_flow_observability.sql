-- Deterministic, replayable lifecycle records for the auth portal MCP endpoint.
-- Request and response bodies, tool arguments, bearer credentials and upstream
-- content are deliberately absent. Identity is copied so the record survives
-- principal or credential deletion.
CREATE TABLE mcp_flows (
    trace_id UUID PRIMARY KEY,
    http_method TEXT NOT NULL,
    request_bytes INTEGER NOT NULL DEFAULT 0 CHECK(request_bytes >= 0),
    response_bytes INTEGER CHECK(response_bytes IS NULL OR response_bytes >= 0),
    rpc_id TEXT,
    rpc_method TEXT,
    tool_name TEXT,
    principal_id INTEGER,
    principal_name TEXT,
    credential_id UUID,
    credential_kind TEXT,
    session_id UUID,
    client_name TEXT,
    provider TEXT CHECK(provider IS NULL OR provider IN ('local','federated')),
    connection_name TEXT,
    upstream_tool TEXT,
    outcome TEXT NOT NULL DEFAULT 'running'
        CHECK(outcome IN ('running','ok','denied','tool_error','protocol_error','upstream_error','error')),
    error_code TEXT,
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ,
    duration_ms INTEGER CHECK(duration_ms IS NULL OR duration_ms >= 0)
);
CREATE INDEX mcp_flows_started_idx ON mcp_flows(started_at DESC);
CREATE INDEX mcp_flows_principal_idx ON mcp_flows(principal_name,started_at DESC);
CREATE INDEX mcp_flows_tool_idx ON mcp_flows(tool_name,started_at DESC)
    WHERE tool_name IS NOT NULL;
CREATE INDEX mcp_flows_outcome_idx ON mcp_flows(outcome,started_at DESC)
    WHERE outcome <> 'ok';

CREATE TABLE mcp_flow_events (
    id BIGSERIAL PRIMARY KEY,
    trace_id UUID NOT NULL REFERENCES mcp_flows(trace_id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    detail JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX mcp_flow_events_cursor_idx ON mcp_flow_events(trace_id,id);
