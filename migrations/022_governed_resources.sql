-- Durable agent artifacts with explicit same-fleet sharing and payload-free events.
CREATE TABLE plane_resources (
 id UUID PRIMARY KEY DEFAULT gen_random_uuid(), path TEXT NOT NULL, version INTEGER NOT NULL,
 title TEXT NOT NULL, media_type TEXT NOT NULL, content BYTEA NOT NULL,
 bytes INTEGER NOT NULL CHECK(bytes >= 0), sha256 TEXT NOT NULL,
 owner_id INTEGER NOT NULL REFERENCES service_accounts(id), owner_name TEXT NOT NULL,
 state TEXT NOT NULL DEFAULT 'active' CHECK(state IN ('active','revoked')),
 source_trace_id UUID NOT NULL, source_connection TEXT, source_tool TEXT,
 parent_id UUID REFERENCES plane_resources(id), expires_at TIMESTAMPTZ,
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(), updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 UNIQUE(path,version)
);
CREATE INDEX plane_resources_owner_time ON plane_resources(owner_id,created_at DESC);
CREATE INDEX plane_resources_path_version ON plane_resources(path,version DESC);
CREATE TABLE plane_resource_grants (
 id UUID PRIMARY KEY DEFAULT gen_random_uuid(), resource_id UUID NOT NULL REFERENCES plane_resources(id) ON DELETE CASCADE,
 principal_id INTEGER NOT NULL REFERENCES service_accounts(id), principal_name TEXT NOT NULL,
 capability TEXT NOT NULL DEFAULT 'read' CHECK(capability='read'),
 granted_by INTEGER NOT NULL REFERENCES service_accounts(id), granted_by_name TEXT NOT NULL,
 granted_at TIMESTAMPTZ NOT NULL DEFAULT now(), expires_at TIMESTAMPTZ,
 revoked_at TIMESTAMPTZ, revoked_by INTEGER REFERENCES service_accounts(id), UNIQUE(resource_id,principal_id)
);
CREATE INDEX plane_resource_grants_principal ON plane_resource_grants(principal_id,resource_id);
CREATE TABLE plane_resource_events (
 id BIGSERIAL PRIMARY KEY, resource_id UUID NOT NULL REFERENCES plane_resources(id) ON DELETE CASCADE,
 trace_id UUID NOT NULL, actor_id INTEGER REFERENCES service_accounts(id), actor_name TEXT NOT NULL,
 kind TEXT NOT NULL, detail JSONB NOT NULL DEFAULT '{}', created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX plane_resource_events_resource_cursor ON plane_resource_events(resource_id,id);
CREATE INDEX plane_resource_events_time ON plane_resource_events(created_at DESC);
