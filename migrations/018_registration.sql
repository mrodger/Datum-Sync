-- 018_registration.sql
--
-- Enrolment: how an agent gets a principal without an operator at a shell.
-- Per spec/agent-auth-plane/03 §4.1 and 06 §019 (numbered 018 here: the
-- agents/disabled drop the spec called 018 is "one release later", and a
-- migration file that sorts before an applied one is the kind of thing
-- migrate.py exists to refuse).
--
-- A registration code is minted by a sponsor (tier >= 3; auto-approve needs
-- tier 4) and carries a TEMPLATE -- the widest authority a registrant may ask
-- for -- bound to a parent. `POST /enrol` with the code creates a `pending`
-- principal under that parent whose requested authority narrows the template
-- (PRIN-001 again, one level earlier), or an `active` one when the code
-- auto-approves. Approval mints nothing: the registrant exchanges a claim
-- code for its first token, once, within POLICY_CLAIM_TTL_HOURS. So the
-- token travels to the process that will use it and to nobody else.
--
-- Only sha256 of a code is stored, as for every other credential here. A
-- code is a credential: it is the one thing an unauthenticated caller can
-- present to get a principal.

CREATE TABLE registration_codes (
    id           SERIAL PRIMARY KEY,
    code_hash    TEXT NOT NULL UNIQUE,
    created_by   INTEGER NOT NULL REFERENCES service_accounts(id) ON DELETE CASCADE,
    -- The sponsor. Every principal enrolled with this code is its child.
    parent_id    INTEGER NOT NULL REFERENCES service_accounts(id) ON DELETE CASCADE,
    -- The authority tuple (grants.py FIELDS) a registrant may narrow.
    template     JSONB NOT NULL,
    auto_approve BOOLEAN NOT NULL DEFAULT false,
    max_uses     INTEGER NOT NULL DEFAULT 1 CHECK (max_uses >= 1),
    uses         INTEGER NOT NULL DEFAULT 0,
    label        TEXT,
    expires_at   TIMESTAMPTZ NOT NULL,
    revoked_at   TIMESTAMPTZ,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (uses <= max_uses)
);

CREATE INDEX registration_codes_parent_idx ON registration_codes (parent_id);

-- One row per enrolment. `claimed_at` is set, never deleted, so "this agent
-- claimed its token on this date" survives and a second claim is recognised
-- as a replay rather than as an unknown code.
CREATE TABLE registration_claims (
    id           SERIAL PRIMARY KEY,
    principal_id INTEGER NOT NULL REFERENCES service_accounts(id) ON DELETE CASCADE,
    code_id      INTEGER NOT NULL REFERENCES registration_codes(id) ON DELETE CASCADE,
    claim_hash   TEXT NOT NULL UNIQUE,
    -- The authority the registrant asked for, kept beside what approval set,
    -- so a reviewer can see the difference.
    requested    JSONB NOT NULL,
    expires_at   TIMESTAMPTZ NOT NULL,
    claimed_at   TIMESTAMPTZ,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX registration_claims_principal_idx ON registration_claims (principal_id);

-- The worker's daily tick restricts idle agents and expires stale
-- registrations. Those rows have an actor, and it is neither an account nor
-- an agent nor an anonymous caller: it is the gateway itself.
ALTER TABLE audit_log DROP CONSTRAINT audit_log_actor_kind_check;
ALTER TABLE audit_log ADD CONSTRAINT audit_log_actor_kind_check
    CHECK (actor_kind = ANY (ARRAY['account', 'agent', 'anonymous', 'system']));
