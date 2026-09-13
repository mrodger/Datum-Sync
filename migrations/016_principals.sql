-- 016_principals.sql
--
-- Every caller is a principal. Per spec/agent-auth-plane/03 §1 and 06 §016.
--
-- Why this exists
-- ---------------
-- `service_accounts` has been the principal table since 001, and `agents`
-- (008) a second identity table beside it whose rows borrow their parent's
-- whole authority at resolve time (auth.py). That is why an agent could not be
-- narrower than its owner: there was no column on the agent to be narrow in.
--
-- This migration gives the principal table what it needs to hold agents as
-- rows of its own -- a kind, a parent, a lifecycle state, proxy grants, limits
-- and a federation block -- and derives `is_admin` from the tier so the two
-- authority signals cannot disagree. 017 then moves the agents across.
--
-- `disabled` is kept for one release. Thirty call sites read it, so it stays a
-- real column, synchronised with `state` by trigger in both directions: an
-- UPDATE that flips `disabled` (the CLI, the PATCH route) moves `state`, and
-- an UPDATE that sets `state` moves `disabled`. Dropped in 018 once the readers
-- have moved to `state`.
--
-- `is_admin` becomes derived: `is_admin = (max_tier >= 4)`, held by a CHECK.
-- Rows that were admin below tier 4 are promoted to tier 4 rather than
-- demoted -- widening an operator's tier is visible on the Principals screen;
-- silently removing admin from one is not. The trigger keeps the rule for
-- every later write, and accepts a write to the flag as shorthand for the
-- tier (true: at least 4; false on an UPDATE: at most 3) so the CLI's
-- `--admin` and the PATCH route keep meaning what they said.
--
-- Agents are never admin. `kind = 'agent'` implies `max_tier <= 3`, as a CHECK,
-- because an agent that could manage principals from an MCP tool call is not
-- the product (spec 03 §6: principal management is tier 4, a human's verb).
-- Delegation from an agent (`delegate_create`) needs tier 3 and stays open.
--
-- `parent_id` cascades. Deleting a sponsor deletes its agents, which is what
-- deleting an account did to its `agents` rows before 017 and what every
-- fixture teardown relies on. The DELETE route refuses a principal with
-- children (CHILDREN_ACTIVE) so that over HTTP the cascade is never reached
-- by accident; the constraint is for the shell, where the operator meant it.
--
-- Revert: drop the trigger, the function, the constraints and the columns.
-- `disabled` and `is_admin` were never dropped and hold correct values.

ALTER TABLE service_accounts
    ADD COLUMN kind              TEXT NOT NULL DEFAULT 'human'
                                 CHECK (kind IN ('human', 'agent', 'system')),
    ADD COLUMN parent_id         INTEGER NULL REFERENCES service_accounts(id) ON DELETE CASCADE,
    ADD COLUMN state             TEXT NOT NULL DEFAULT 'active'
                                 CHECK (state IN ('pending', 'active', 'restricted',
                                                  'disabled', 'retired', 'rejected')),
    ADD COLUMN proxy_grants      TEXT[] NOT NULL DEFAULT '{}',
    ADD COLUMN limits            JSONB NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN federation_scope  JSONB NULL,
    ADD COLUMN metadata          JSONB NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN restricted_from   JSONB NULL,
    ADD COLUMN restricted_reason TEXT NULL,
    ADD COLUMN review_due_at     TIMESTAMPTZ NULL,
    ADD COLUMN last_reviewed_at  TIMESTAMPTZ NULL,
    ADD COLUMN last_reviewed_by  TEXT NULL,
    ADD COLUMN created_by        TEXT NULL;

-- Existing rows: the boolean becomes the state.
UPDATE service_accounts SET state = 'disabled' WHERE disabled;

-- Admin below tier 4 is promoted, not demoted. Then the derivation holds for
-- every row and the CHECK can be added.
UPDATE service_accounts SET max_tier = 4 WHERE is_admin AND max_tier < 4;
UPDATE service_accounts SET is_admin = (max_tier >= 4);

ALTER TABLE service_accounts
    ADD CONSTRAINT service_accounts_admin_is_tier CHECK (is_admin = (max_tier >= 4)),
    ADD CONSTRAINT service_accounts_agent_tier CHECK (kind <> 'agent' OR max_tier <= 3),
    ADD CONSTRAINT service_accounts_no_self_parent CHECK (parent_id IS NULL OR parent_id <> id);

CREATE FUNCTION service_accounts_sync() RETURNS trigger AS $$
BEGIN
    -- Shorthand, for writers that still speak in the flag: an INSERT or an
    -- UPDATE that sets `is_admin` moves the tier to match (true -> at least
    -- 4, false -> at most 3). An UPDATE that sets the tier and leaves the
    -- flag alone is not shorthand, and the flag follows the tier below.
    IF TG_OP = 'INSERT' OR NEW.is_admin IS DISTINCT FROM OLD.is_admin THEN
        IF NEW.is_admin AND NEW.max_tier < 4 THEN
            NEW.max_tier := 4;
        ELSIF NOT NEW.is_admin AND NEW.max_tier >= 4 AND TG_OP = 'UPDATE' THEN
            NEW.max_tier := 3;
        END IF;
    END IF;
    NEW.is_admin := NEW.max_tier >= 4;

    IF TG_OP = 'INSERT' THEN
        -- An INSERT that says `disabled = true` and nothing about state means
        -- the disabled state; otherwise state is authoritative.
        IF NEW.disabled AND NEW.state = 'active' THEN
            NEW.state := 'disabled';
        END IF;
    ELSIF NEW.disabled IS DISTINCT FROM OLD.disabled AND NEW.state = OLD.state THEN
        -- The writer flipped the boolean (CLI `disable`/`enable`, PATCH).
        IF NEW.disabled THEN
            NEW.state := 'disabled';
        ELSIF OLD.state = 'disabled' THEN
            NEW.state := 'active';
        END IF;
    END IF;
    NEW.disabled := NEW.state NOT IN ('active', 'restricted');
    RETURN NEW;
END $$ LANGUAGE plpgsql;

CREATE TRIGGER service_accounts_sync
    BEFORE INSERT OR UPDATE ON service_accounts
    FOR EACH ROW EXECUTE FUNCTION service_accounts_sync();

CREATE INDEX service_accounts_parent_idx ON service_accounts (parent_id);
CREATE INDEX service_accounts_state_idx  ON service_accounts (state) WHERE state <> 'active';
CREATE INDEX service_accounts_review_idx ON service_accounts (review_due_at)
    WHERE review_due_at IS NOT NULL;

-- A per-token tier ceiling: the baseline credential of spec 02 §3. NULL means
-- the token carries the principal's own tier. Never above it -- enforced in
-- tokens.create, and in auth.resolve by taking the minimum, so a row edited by
-- hand above the principal's tier still cannot raise it.
ALTER TABLE account_tokens ADD COLUMN max_tier INTEGER NULL
    CHECK (max_tier BETWEEN 1 AND 5);
