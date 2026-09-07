-- 014_anon_audit.sql
--
-- A failed login attempt is exactly the event an audit trail should capture,
-- but audit_log.actor_id is NOT NULL, so a row for an unverified caller cannot
-- be written at all. The gap was noted at C1 and is closed here.
--
-- The change
-- ----------
-- `actor_id` becomes nullable. An anonymous row carries the submitted name in
-- `actor_name` (unverified -- it is what the caller claimed) and NULL for the
-- id. A NULL id is unambiguous: the FK target is service_accounts.id, which
-- starts at 1, so there is no real account this can be confused with.
--
-- `actor_kind` gains an 'anonymous' value for the same rows. Keeping it NOT
-- NULL preserves the invariant that every row says what kind of actor produced
-- it; making it nullable would require every reader to handle a third state
-- (NULL, 'account', 'agent') where two already cover all authenticated cases.
--
-- The existing check constraint is replaced rather than extended in place
-- because ALTER TABLE ... DROP CONSTRAINT ... ADD CONSTRAINT is two DDL
-- statements and Postgres applies them transactionally, so either both succeed
-- or the column stays as it was.

ALTER TABLE audit_log ALTER COLUMN actor_id DROP NOT NULL;

ALTER TABLE audit_log DROP CONSTRAINT audit_log_actor_kind_check;
ALTER TABLE audit_log ADD CONSTRAINT audit_log_actor_kind_check
    CHECK (actor_kind = ANY (ARRAY['account', 'agent', 'anonymous']));
