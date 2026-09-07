-- 015_drop_stale_cols.sql
--
-- Three columns on service_accounts that were superseded by earlier migrations
-- and have not been read since:
--
--   token_hash     -- superseded by account_tokens (migration 012). The
--                     backfill there moved existing hashes into account_tokens
--                     as label='legacy' rows. auth.py's read path never
--                     consults this column; tokens.py is the only read path.
--                     The UNIQUE constraint and index on it drop with the column.
--
--   token_expires  -- was paired with token_hash; same story.
--
--   connection_grants -- removed from code in B3a (migration 013 backport).
--                        The column was never read to enforce anything; whether
--                        a workspace may use a connection is settled by the
--                        connection's own scope. tokens.py already notes its
--                        absence from _ACCOUNT_COLS explicitly.

ALTER TABLE service_accounts
    DROP COLUMN token_hash,
    DROP COLUMN token_expires,
    DROP COLUMN connection_grants;
