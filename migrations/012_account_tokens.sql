-- 012_account_tokens.sql
--
-- More than one live token per principal, each with a label an operator can
-- name. Per spec/datum-gate/03-authority.md and the B5 entry in
-- spec/datum-gate/_BACKPORT-PLAN.md.
--
-- Why this table exists
-- ---------------------
-- `service_accounts.token_hash` is a single column with a UNIQUE constraint,
-- so an account has exactly one token and rotation is `UPDATE ... SET
-- token_hash = $2` (datum_sync/accounts.py, `mint`). Every holder of the old
-- value stops working at the instant of that write, and there is no window in
-- which both the old and the new token are accepted. For any account with more
-- than one process behind it there is therefore no safe rotation at all: the
-- CLI even says so out loud -- "The previous token for this account no longer
-- works."
--
-- With a child table, rotation is: add a token, deploy it, revoke the old one.
-- Two writes, with a window between them that the operator controls.
--
-- `label` is what makes revocation usable. Revoking by hash requires knowing
-- which hash, and the hash is exactly the thing the database cannot give back.
-- An operator revokes "the CI runner".
--
-- The old column is not dropped here
-- ----------------------------------
-- The backfill below copies every existing hash into this table, and
-- `auth.resolve` then reads THIS TABLE ONLY -- `service_accounts.token_hash`
-- is left in place, still populated, and never consulted again.
--
-- It is not read, because a fallback to it would make revocation a lie: the
-- backfilled 'legacy' row and the column hold the SAME hash, so revoking the
-- row while a fallback branch still matched the column would return 200 to a
-- credential an operator had just revoked. That failure is silent and is
-- exactly the class of thing this repository has been bitten by before.
--
-- It is not dropped, because leaving it populated is what makes this migration
-- reversible without a down migration: reverting the code restores the old read
-- path against data that was never disturbed. Dropping the column is a later
-- migration, once this path has run in production for a while.
--
-- The consequence to keep in mind: from this migration onwards the column is
-- STALE. It is not the credential and must not be displayed as one --
-- `has_token` in accounts.py and api.py is repointed at this table in the same
-- change, or an account with three live tokens would render as having none.
--
-- What is never stored: the raw token. Only sha256(token) hex, same as the
-- column it replaces and same as `agents.token_hash` and `oauth_tokens`.

CREATE TABLE account_tokens (
    id           SERIAL PRIMARY KEY,
    account_id   INTEGER NOT NULL
                     REFERENCES service_accounts(id) ON DELETE CASCADE,

    -- Operator-facing name: 'ci-runner', 'laptop', 'legacy'. Not a secret and
    -- not an authentication input -- it is only ever how a human says which
    -- token to revoke.
    label        TEXT NOT NULL,

    -- sha256(token) hex. UNIQUE across every account, not per account: a
    -- presented token is resolved by hash alone, with no account named, so two
    -- accounts sharing a hash would make that lookup ambiguous.
    token_hash   TEXT NOT NULL UNIQUE,

    -- NULL means no expiry, matching service_accounts.token_expires.
    expires_at   TIMESTAMPTZ,

    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_used_at TIMESTAMPTZ,

    -- Set, never deleted. A revoked token keeps its row so that "this
    -- credential was withdrawn on this date" survives, and so an audit trail
    -- referring to it still resolves.
    revoked_at   TIMESTAMPTZ
);

-- Labels are unique among LIVE tokens only. Unique outright would mean a label
-- could be used once ever, so revoking 'ci-runner' would permanently burn the
-- only name anyone would think to give its replacement.
CREATE UNIQUE INDEX account_tokens_live_label_idx
    ON account_tokens (account_id, label) WHERE revoked_at IS NULL;

-- The lookup on every authenticated request.
CREATE INDEX account_tokens_hash_idx ON account_tokens (token_hash);

-- Listing an account's tokens, and the live-token count behind `has_token`.
CREATE INDEX account_tokens_account_idx ON account_tokens (account_id);


-- Backfill. In the same transaction as the CREATE (migrate.py wraps each file
-- in one), so there is no instant at which the table exists and an account's
-- existing token is not in it. This is what makes the change invisible to
-- every current token holder: the credential in their environment keeps
-- working, under the label 'legacy'.
--
-- created_at and last_used_at are carried across rather than defaulted. They
-- are the only record of how old the credential is and whether anything still
-- uses it, which is the first question asked when deciding what to revoke.
INSERT INTO account_tokens
    (account_id, label, token_hash, expires_at, created_at, last_used_at)
SELECT id, 'legacy', token_hash, token_expires, created_at, last_used_at
  FROM service_accounts
 WHERE token_hash IS NOT NULL;
