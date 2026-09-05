-- Widen the tier ceiling from 4 to 5.
--
-- WHY THIS EXISTS
-- 001_core.sql and 005_connections.sql both declare CHECK (... BETWEEN 1 AND 4).
-- The tier-5 superuser shipped in e98db82 and the application has assumed a
-- 1..5 range ever since (api.py, connections.py, accounts.py), but the widening
-- was performed directly against the database and never written as a migration.
-- The running database therefore said 1..5 while the repository said 1..4, and a
-- rebuild from migrations produced a database the application would reject at
-- tier 5. This file closes that gap: it is the missing step, written down.
--
-- The two source files are already applied and checksummed, so editing them in
-- place would trip the drift guard in datum_sync/migrate.py - correctly. A new
-- migration is the only honest way to record a change to an applied schema.
--
-- IDEMPOTENCE
-- DROP ... IF EXISTS then ADD means this yields the same end state whether it
-- runs against a database already widened by hand or a fresh one built from
-- 001 upward. The constraint names are PostgreSQL's own defaults, which is what
-- both the hand-altered database and a fresh build produce.

ALTER TABLE service_accounts
    DROP CONSTRAINT IF EXISTS service_accounts_max_tier_check;
ALTER TABLE service_accounts
    ADD CONSTRAINT service_accounts_max_tier_check
    CHECK (max_tier >= 1 AND max_tier <= 5);

ALTER TABLE connections
    DROP CONSTRAINT IF EXISTS connections_tier_check;
ALTER TABLE connections
    ADD CONSTRAINT connections_tier_check
    CHECK (tier >= 1 AND tier <= 5);
