-- 023_retire_mirrors.sql
--
-- The per-surface logs that predate the audit spine are retired
-- (spec/agent-auth-plane/06 §023, 09 WP7). `audit_log` has carried every row
-- they did since 011, with the trace, the session and the actor as a
-- principal; the verification is recorded in
-- spec/agent-auth-plane/_verify-audit-mirror.md, and the one disagreement
-- (a refused federated call is `denied` here and was `ok` there) is the
-- reason they go rather than a reason to keep them.

DROP TABLE mcp_call_log;
DROP TABLE proxy_log;
