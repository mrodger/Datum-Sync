-- Additive prototype authority plane. No existing account or agent is promoted,
-- renamed or issued a credential. Revert by dropping plane_* tables and the
-- three portal columns only after exporting their audit and principal records.
ALTER TABLE service_accounts ADD COLUMN portal_kind TEXT NOT NULL DEFAULT 'legacy'
    CHECK (portal_kind IN ('legacy','human','agent'));
ALTER TABLE service_accounts ADD COLUMN portal_parent INTEGER REFERENCES service_accounts(id);
ALTER TABLE service_accounts ADD COLUMN portal_state TEXT NOT NULL DEFAULT 'active'
    CHECK (portal_state IN ('pending','active','restricted','disabled','retired','rejected'));
ALTER TABLE service_accounts ADD COLUMN portal_grant JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE service_accounts ADD COLUMN portal_metadata JSONB NOT NULL DEFAULT '{}'::jsonb;

CREATE TABLE plane_credentials (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    principal_id INTEGER NOT NULL REFERENCES service_accounts(id) ON DELETE CASCADE,
    token_hash TEXT UNIQUE NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('session','baseline','access','refresh')),
    label TEXT NOT NULL,
    tier INTEGER NOT NULL CHECK (tier BETWEEN 1 AND 4),
    scope TEXT NOT NULL DEFAULT 'mcp',
    family UUID NOT NULL DEFAULT gen_random_uuid(),
    client_id TEXT,
    expires_at TIMESTAMPTZ NOT NULL,
    authorization_expires_at TIMESTAMPTZ NOT NULL,
    revoked_at TIMESTAMPTZ,
    consumed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_used_at TIMESTAMPTZ
);
CREATE INDEX plane_credentials_principal ON plane_credentials(principal_id);
CREATE INDEX plane_credentials_family ON plane_credentials(family);

CREATE TABLE plane_enrolments (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    sponsor_id INTEGER NOT NULL REFERENCES service_accounts(id),
    code_hash TEXT UNIQUE NOT NULL,
    template JSONB NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    used_at TIMESTAMPTZ,
    principal_id INTEGER REFERENCES service_accounts(id),
    claim_hash TEXT UNIQUE,
    claimed_at TIMESTAMPTZ,
    claim_expires_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE plane_clients (
    client_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    redirect_uris JSONB NOT NULL DEFAULT '[]',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
INSERT INTO plane_clients(client_id,name) VALUES ('datum-local','Datum local client');

CREATE TABLE plane_authorizations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    principal_id INTEGER NOT NULL REFERENCES service_accounts(id),
    client_id TEXT NOT NULL REFERENCES plane_clients(client_id),
    device_hash TEXT UNIQUE,
    user_code TEXT UNIQUE,
    code_hash TEXT UNIQUE,
    challenge TEXT,
    redirect_uri TEXT,
    scope TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','approved','denied','consumed')),
    requested_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL,
    duration_seconds INTEGER NOT NULL CHECK(duration_seconds BETWEEN 60 AND 3600),
    authorized_until TIMESTAMPTZ,
    decided_by INTEGER REFERENCES service_accounts(id),
    decided_at TIMESTAMPTZ,
    last_polled_at TIMESTAMPTZ,
    poll_interval INTEGER NOT NULL DEFAULT 5,
    family UUID NOT NULL DEFAULT gen_random_uuid()
);

CREATE TABLE plane_sessions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    principal_id INTEGER NOT NULL REFERENCES service_accounts(id),
    credential_id UUID NOT NULL REFERENCES plane_credentials(id),
    client_name TEXT NOT NULL,
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL,
    ended_at TIMESTAMPTZ
);
CREATE INDEX plane_sessions_live ON plane_sessions(principal_id) WHERE ended_at IS NULL;

CREATE TABLE plane_documents (
    path TEXT PRIMARY KEY,
    content TEXT NOT NULL,
    updated_by INTEGER REFERENCES service_accounts(id),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
INSERT INTO plane_documents(path,content) VALUES
('demo/welcome.md','Welcome to Datum. This document is readable with a baseline credential. Elevate to edit it.'),
('demo/release-notes.md','Draft release notes. Publishing requires a separate human approval.'),
('private/operator.md','This document is excluded from the demonstration agent grant.');

CREATE TABLE plane_pending_calls (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    principal_id INTEGER NOT NULL REFERENCES service_accounts(id),
    credential_id UUID NOT NULL REFERENCES plane_credentials(id),
    tool TEXT NOT NULL,
    args JSONB NOT NULL,
    grant_snapshot JSONB NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','executed','denied','cancelled')),
    requested_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL,
    decided_by INTEGER REFERENCES service_accounts(id),
    decided_at TIMESTAMPTZ,
    result JSONB
);
CREATE TABLE plane_releases (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    pending_id UUID UNIQUE NOT NULL REFERENCES plane_pending_calls(id),
    path TEXT NOT NULL,
    content TEXT NOT NULL,
    principal_id INTEGER NOT NULL REFERENCES service_accounts(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE plane_audit (
    id BIGSERIAL PRIMARY KEY,
    trace_id UUID NOT NULL,
    actor_id INTEGER REFERENCES service_accounts(id),
    actor_name TEXT NOT NULL,
    verb TEXT NOT NULL,
    target TEXT NOT NULL,
    outcome TEXT NOT NULL CHECK(outcome IN ('ok','denied','error')),
    detail JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX plane_audit_time ON plane_audit(created_at DESC);
