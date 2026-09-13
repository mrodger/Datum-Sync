-- 020_device_flow.sql
--
-- The device authorisation grant (RFC 8628) for headless agents, and a
-- `denied` audit outcome. Per spec/agent-auth-plane/04 §3 and 06 §021.
--
-- A device code is the agent's half of an elevation: it asks with its
-- baseline token, a person approves on the Approvals screen (or the policy
-- auto-approves a scope that unlocks nothing), and the agent polls the token
-- endpoint until the tokens are minted. The row holds the requesting
-- principal, so an approval can never bind tokens to anyone else, and the
-- decision, so the poll can tell "not yet" from "no".
--
-- Only sha256 of the device code is stored. The user code is not a secret:
-- it is shown on a screen beside the principal's name so the approver can
-- match it to what the agent's operator reads out.

CREATE TABLE oauth_device_codes (
    id               SERIAL PRIMARY KEY,
    device_code_hash TEXT NOT NULL UNIQUE,
    user_code        TEXT NOT NULL,
    client_id        TEXT NOT NULL REFERENCES oauth_clients(client_id) ON DELETE CASCADE,
    principal_id     INTEGER NOT NULL REFERENCES service_accounts(id) ON DELETE CASCADE,
    scope            TEXT NOT NULL,
    resource         TEXT NOT NULL,
    interval_seconds INTEGER NOT NULL DEFAULT 5,
    requested_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at       TIMESTAMPTZ NOT NULL,
    last_polled_at   TIMESTAMPTZ,
    decision         TEXT CHECK (decision IN ('approved', 'denied')),
    approved_scope   TEXT,                        -- narrower than `scope`, never wider
    decided_by       INTEGER,
    decided_name     TEXT,
    decided_at       TIMESTAMPTZ,
    reason           TEXT,
    consumed_at      TIMESTAMPTZ
);

-- Undecided user codes are unique; a decided one may be reissued.
CREATE UNIQUE INDEX oauth_device_codes_user_code_idx
    ON oauth_device_codes (user_code) WHERE decision IS NULL;
CREATE INDEX oauth_device_codes_principal_idx ON oauth_device_codes (principal_id, requested_at DESC);

-- 'denied' becomes writable. The first writer is the device flow (an
-- elevation a person refused); federation guards (WP5) are the second.
-- mcp_call_log is not widened; its disagreement with audit_log from here on
-- is the recorded reason it retires (spec 11 D-18).
ALTER TABLE audit_log DROP CONSTRAINT audit_log_outcome_check;
ALTER TABLE audit_log ADD CONSTRAINT audit_log_outcome_check
    CHECK (outcome IN ('ok', 'error', 'denied'));
