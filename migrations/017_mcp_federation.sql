-- First incremental legacy integration: MCP connections and a persistent,
-- process-independent tool catalogue. Existing identities and grants are not
-- widened by this migration.
ALTER TABLE connections DROP CONSTRAINT connections_type_check;
ALTER TABLE connections ADD CONSTRAINT connections_type_check
    CHECK (type IN ('database','http','email_smtp','email_imap','file','oauth_client','mcp'));
ALTER TABLE connections ADD COLUMN federation_status JSONB NOT NULL DEFAULT '{}'::jsonb;

CREATE TABLE federated_tools (
    connection TEXT NOT NULL REFERENCES connections(name) ON DELETE CASCADE,
    upstream_name TEXT NOT NULL,
    tool_name TEXT NOT NULL UNIQUE,
    description TEXT NOT NULL DEFAULT '',
    input_schema JSONB NOT NULL DEFAULT '{"type":"object","properties":{}}'::jsonb,
    annotations JSONB NOT NULL DEFAULT '{}'::jsonb,
    fetched_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY(connection, upstream_name)
);
CREATE INDEX federated_tools_connection ON federated_tools(connection);
