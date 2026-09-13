-- 022_pending_calls.sql
--
-- Approval-gated federated calls (spec/agent-auth-plane/05 §5, 06 §022).
--
-- The one place argument *values* are stored: an approver must see what
-- they approve. The row carries the requester's grant snapshot, taken at
-- request time, and the approved call runs under that snapshot rather than
-- whatever the requester holds by the time a person gets to it.

CREATE TABLE pending_calls (
    id              BIGSERIAL PRIMARY KEY,
    connection      TEXT NOT NULL,
    tool            TEXT NOT NULL,
    args            JSONB NOT NULL,
    label           TEXT NOT NULL,
    requested_by    INTEGER NOT NULL,
    requested_name  TEXT NOT NULL,
    requested_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at      TIMESTAMPTZ NOT NULL,
    session_id      UUID,
    trace_id        UUID NOT NULL,
    grant_snapshot  JSONB NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending','approved','rejected','expired','executed','failed')),
    decided_by      INTEGER,
    decided_name    TEXT,
    decided_at      TIMESTAMPTZ,
    reason          TEXT,
    result          JSONB
);
CREATE INDEX pending_calls_open_idx ON pending_calls (requested_at) WHERE status = 'pending';
CREATE INDEX pending_calls_requester_idx ON pending_calls (requested_by, requested_at DESC);
