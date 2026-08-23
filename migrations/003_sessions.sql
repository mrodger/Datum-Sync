-- Browser sessions for the web UI.
--
-- A person at the UI has no bearer token to present, so signing in mints an
-- opaque session credential carried in a cookie. It lives in oauth_tokens
-- beside the access and refresh tokens rather than in a table of its own: the
-- lifecycle is identical -- sha256 in, expiry, revoke, last_used_at -- so a
-- separate table would duplicate every query and, worse, split the answer to
-- "what credentials currently exist for this account" across two places. Sign
-- out and admin revocation come free.
--
-- client_id stays NULL: a session is not issued to an OAuth client, and
-- `resource` stays NULL because a cookie is only ever presented to the origin
-- that set it, so there is no audience to bind.
--
-- The constraint is auto-named by Postgres from the inline CHECK in
-- 002_auth.sql. Verified against pg_constraint rather than assumed.

ALTER TABLE oauth_tokens DROP CONSTRAINT oauth_tokens_kind_check;

ALTER TABLE oauth_tokens
    ADD CONSTRAINT oauth_tokens_kind_check
    CHECK (kind IN ('access', 'refresh', 'session'));
