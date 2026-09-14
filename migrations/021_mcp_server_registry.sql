-- Governed MCP server lifecycle, per-tool controls, durable server events and
-- a small read-only dataset for the local PostgreSQL MCP demonstration.
ALTER TABLE connections ADD COLUMN portal_state TEXT NOT NULL DEFAULT 'active'
    CHECK(portal_state IN ('active','disabled','retired'));
ALTER TABLE connections ADD COLUMN provider_kind TEXT NOT NULL DEFAULT 'custom';
ALTER TABLE connections ADD COLUMN display_name TEXT;
ALTER TABLE connections ADD COLUMN owner_id INTEGER REFERENCES service_accounts(id);

ALTER TABLE federated_tools ADD COLUMN enabled BOOLEAN NOT NULL DEFAULT true;
ALTER TABLE federated_tools ADD COLUMN min_tier INTEGER NOT NULL DEFAULT 1 CHECK(min_tier BETWEEN 1 AND 4);

CREATE TABLE mcp_server_events (
    id BIGSERIAL PRIMARY KEY,
    connection_name TEXT NOT NULL,
    actor_id INTEGER,
    actor_name TEXT,
    trace_id UUID,
    kind TEXT NOT NULL,
    detail JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX mcp_server_events_connection_cursor ON mcp_server_events(connection_name,id);
CREATE INDEX mcp_server_events_time ON mcp_server_events(created_at DESC);

CREATE SCHEMA mcp_demo;
CREATE TABLE mcp_demo.orders (
    id INTEGER PRIMARY KEY,
    ordered_on DATE NOT NULL,
    region TEXT NOT NULL,
    product TEXT NOT NULL,
    status TEXT NOT NULL,
    total_nzd NUMERIC(12,2) NOT NULL
);
INSERT INTO mcp_demo.orders(id,ordered_on,region,product,status,total_nzd) VALUES
    (1001,'2026-08-28','Auckland','Field Survey','complete',1840.00),
    (1002,'2026-09-02','Wellington','Data Cleanup','complete',965.50),
    (1003,'2026-09-06','Canterbury','Asset Inspection','processing',2740.00),
    (1004,'2026-09-09','Auckland','Map Publication','pending',620.00),
    (1005,'2026-09-11','Otago','Field Survey','complete',1535.75),
    (1006,'2026-09-13','Wellington','Asset Inspection','pending',2190.00);
