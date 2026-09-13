-- 021_federation.sql
--
-- Upstream MCP servers as connections, and the cached catalogue of their
-- tools. Per spec/agent-auth-plane/05 §1 and §3 and 06 §022 (numbered 021
-- here: D-31, the build order is the numbering).
--
-- The mapping from a gateway tool name to an upstream tool is a table, not a
-- process-local dict, so a tools/call on any process resolves the same name
-- and an upstream that is down still serves its last catalogue.

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
    name          TEXT,
    description   TEXT,
    mime_type     TEXT,
    is_template   BOOLEAN NOT NULL DEFAULT false,
    fetched_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (connection, upstream_uri)
);
