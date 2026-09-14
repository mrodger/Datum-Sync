-- Write-only upstream credentials, versioned secret material, explicit agent
-- use grants, reviewable requests, and payload-free lifecycle events.
CREATE TABLE auth_credentials (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    connection_name TEXT NOT NULL UNIQUE REFERENCES connections(name) ON DELETE CASCADE,
    label TEXT NOT NULL,
    auth_type TEXT NOT NULL CHECK(auth_type IN ('bearer','basic','header','query_param','oauth_client','none')),
    owner_name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','disabled','retired')),
    current_version_id UUID,
    expires_at TIMESTAMPTZ,
    last_used_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE auth_credential_versions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    credential_id UUID NOT NULL REFERENCES auth_credentials(id) ON DELETE CASCADE,
    version INTEGER NOT NULL CHECK(version > 0),
    ciphertext BYTEA NOT NULL,
    aad TEXT NOT NULL,
    key_id INTEGER NOT NULL CHECK(key_id BETWEEN 0 AND 255),
    fingerprint TEXT NOT NULL,
    secret_fields TEXT[] NOT NULL DEFAULT '{}',
    state TEXT NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','active','retired','failed')),
    created_by INTEGER REFERENCES service_accounts(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    activated_at TIMESTAMPTZ,
    retired_at TIMESTAMPTZ,
    UNIQUE(credential_id,version)
);
ALTER TABLE auth_credentials ADD CONSTRAINT auth_credentials_current_version_fk
    FOREIGN KEY(current_version_id) REFERENCES auth_credential_versions(id);

CREATE TABLE credential_requests (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    requested_by INTEGER REFERENCES service_accounts(id),
    requested_by_name TEXT NOT NULL,
    credential_id UUID NOT NULL REFERENCES auth_credentials(id),
    requested_tools TEXT[] NOT NULL,
    requested_methods TEXT[] NOT NULL DEFAULT '{}',
    requested_paths TEXT[] NOT NULL DEFAULT '{}',
    duration_seconds INTEGER NOT NULL CHECK(duration_seconds BETWEEN 300 AND 2592000),
    purpose TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','approved','denied','cancelled','expired')),
    requested_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL DEFAULT now()+interval '24 hours',
    reviewed_by INTEGER REFERENCES service_accounts(id),
    reviewed_at TIMESTAMPTZ,
    decision_detail JSONB NOT NULL DEFAULT '{}'
);
CREATE UNIQUE INDEX credential_requests_one_pending
    ON credential_requests(requested_by,credential_id) WHERE status='pending';

CREATE TABLE credential_grants (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    principal_id INTEGER REFERENCES service_accounts(id),
    principal_name TEXT NOT NULL,
    credential_id UUID NOT NULL REFERENCES auth_credentials(id),
    allowed_tools TEXT[] NOT NULL,
    allowed_methods TEXT[] NOT NULL DEFAULT '{}',
    allowed_paths TEXT[] NOT NULL DEFAULT '{}',
    source TEXT NOT NULL CHECK(source IN ('request','direct')),
    request_id UUID REFERENCES credential_requests(id),
    state TEXT NOT NULL DEFAULT 'active' CHECK(state IN ('active','revoked','expired')),
    valid_from TIMESTAMPTZ NOT NULL DEFAULT now(),
    valid_until TIMESTAMPTZ,
    granted_by INTEGER REFERENCES service_accounts(id),
    granted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    revoked_by INTEGER REFERENCES service_accounts(id),
    revoked_at TIMESTAMPTZ,
    revoke_reason TEXT
);
CREATE INDEX credential_grants_principal_live ON credential_grants(principal_id,credential_id)
    WHERE state='active';
CREATE INDEX credential_grants_credential_live ON credential_grants(credential_id,principal_id)
    WHERE state='active';

CREATE TABLE credential_events (
    id BIGSERIAL PRIMARY KEY,
    credential_id UUID NOT NULL,
    credential_label TEXT NOT NULL,
    principal_id INTEGER,
    principal_name TEXT,
    trace_id UUID,
    kind TEXT NOT NULL,
    detail JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX credential_events_credential_cursor ON credential_events(credential_id,id);
CREATE INDEX credential_events_principal_time ON credential_events(principal_id,created_at DESC);
