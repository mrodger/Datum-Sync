-- 017_fold_agents.sql
--
-- Agents become principals. Per spec/agent-auth-plane/03 §1.1 and 06 §017.
--
-- One `service_accounts` row per `agents` row: kind 'agent', parent the
-- account, the same name (both columns are UNIQUE, and an agent sharing a name
-- with an account would have been ambiguous on every screen already). The
-- authority columns are copied from the parent, capped at tier 3 by the CHECK
-- from 016, so that nothing narrows on the day this runs: an agent token
-- resolved to its parent's authority before (auth.py's agent branch) and
-- resolves to a copy of it after. Narrowing is then an operator's edit, made
-- on the Principals screen with the parent's values beside each field.
--
-- `tools/audit_tier_report.py` lists every agent this preserves at tier 3.
--
-- The parent's `proxy_grants` becomes the union of its agents' grants. Under
-- the narrowing rule (grants.narrows) a child may hold only what its parent
-- holds, and before this migration accounts held no proxy grants at all --
-- the column did not exist. Without this step every migrated agent would
-- violate PRIN-001 on its first edit.
--
-- The agent's token moves to `account_tokens` under the label 'agent', with
-- its timestamps, so the credential in every agent's environment keeps
-- working and its age is not lost. `auth.resolve` stops reading `agents` in
-- the same change; the table is not dropped here -- as 012 did with
-- service_accounts.token_hash, it stays for one release as the revert path,
-- and 018 drops it.

INSERT INTO service_accounts
    (name, description, kind, parent_id, state, max_tier, repo_scope, vault_scope,
     proxy_grants, rate_limit_per_min, is_admin, created_at, last_used_at, created_by)
SELECT ag.name,
       'migrated from agents (017)',
       'agent',
       ag.account_id,
       CASE WHEN ag.disabled OR sa.disabled THEN 'disabled' ELSE 'active' END,
       LEAST(sa.max_tier, 3),
       sa.repo_scope,
       sa.vault_scope,
       ag.proxy_grants,
       sa.rate_limit_per_min,
       false,
       ag.created_at,
       ag.last_used_at,
       sa.name
  FROM agents ag
  JOIN service_accounts sa ON sa.id = ag.account_id;

UPDATE service_accounts sa
   SET proxy_grants = u.grants
  FROM (
        SELECT account_id, array_agg(DISTINCT g) AS grants
          FROM agents, unnest(proxy_grants) AS g
         GROUP BY account_id
       ) u
 WHERE sa.id = u.account_id;

INSERT INTO account_tokens (account_id, label, token_hash, created_at, last_used_at)
SELECT p.id, 'agent', ag.token_hash, ag.created_at, ag.last_used_at
  FROM agents ag
  JOIN service_accounts p ON p.name = ag.name AND p.kind = 'agent'
 WHERE ag.token_hash IS NOT NULL;
