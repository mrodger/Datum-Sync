# 22 — Agent Lifecycle: Registration, Authorisation, Review, Retirement

An agent is "barebones standalone" until it is registered and authorised.
This document is the path from a process with nothing to a member of the
organisation, and back out again.

## 1. States

```
                 ┌──────────┐  approve   ┌──────────┐  restrict  ┌────────────┐
  register ─────►│ pending  ├───────────►│  active  ├───────────►│ restricted │  (tier 1, reversible)
                 └────┬─────┘            └────┬─────┘◄───────────┴────────────┘
                      │ reject                │ disable          restore
                      ▼                       ▼
                 ┌──────────┐            ┌──────────┐  retire    ┌──────────┐
                 │ rejected │            │ disabled ├───────────►│ retired  │  (credentials purged, history kept)
                 └──────────┘            └──────────┘            └──────────┘
```

`principals.state` (migration `011_lifecycle.sql`) replaces the bare
`disabled` boolean:

```sql
ALTER TABLE principals ADD COLUMN state TEXT NOT NULL DEFAULT 'active'
    CHECK (state IN ('pending','active','restricted','disabled','retired','rejected'));
ALTER TABLE principals ADD COLUMN restricted_from JSONB;   -- the grant before restriction, for restore
ALTER TABLE principals ADD COLUMN review_due_at TIMESTAMPTZ;
ALTER TABLE principals ADD COLUMN last_reviewed_at TIMESTAMPTZ;
ALTER TABLE principals ADD COLUMN last_reviewed_by TEXT;
ALTER TABLE principals ADD COLUMN metadata JSONB NOT NULL DEFAULT '{}'::jsonb;  -- model, host, owner contact, purpose
ALTER TABLE principals DROP COLUMN disabled;   -- second release; 'disabled' state replaces it
```

Only `active` authenticates normally. `restricted` authenticates with an
effective grant forced to tier 1 with empty write/proxy/code/compute/
documents blocks (the README's "revoke drops to tier 1"). `pending`,
`disabled`, `retired`, `rejected` → 401 with the state as the code.

## 2. Registration

Two entry points, both producing a `pending` principal:

**By a sponsor** (tier ≥3 for agents ≤ tier 2; tier ≥4 otherwise):
`POST /rest/v1/principals` with `state: "pending"` or the MCP
`delegate_create` — the latter auto-approves within the sponsor's own
narrowing envelope because the sponsor already holds that authority.

**Self-registration** (`POST /register`, public, rate-limited by IP and by
`registration_code`):

```json
{"registration_code": "…", "name": "drone-7f2a", "kind": "agent",
 "metadata": {"model": "claude-opus-5", "host": "vm111", "owner": "marcus", "purpose": "literature review"},
 "requested_grant": {…}}
```

- `registration_codes` (table: code hash, created_by, max_uses, expires_at,
  `template_grant`, `auto_approve` bool, `parent_id`) are minted by tier ≥4.
  A code binds the registrant to a parent and a **template grant**; the
  `requested_grant` must narrow the template, else 400.
- With `auto_approve`, the principal is created `active` under the
  template's parent and a token is returned once. Without it, the
  principal is `pending`, no token is issued, and the response carries a
  `claim_code`.
- Approval (`POST /rest/v1/principals/{name}/approve`, tier ≥4 with the
  parent in their subtree) may edit the grant, sets `active`, and issues the
  first token — returned to the approver, or exchanged by the registrant
  via `POST /register/claim {claim_code}` within 24 h. Rejection records a
  reason.

Every registration creates the agent's private memory namespace
`agents/{name}` and adds `agents/{name}/**` to its memory read/write.

## 3. Review

`policy.lifecycle`:

```yaml
lifecycle:
  review_interval_days: 90          # active agents must be reviewed
  inactivity_restrict_days: 30      # no request for 30d → restricted
  inactivity_disable_days: 90
  pending_ttl_days: 7               # unapproved registrations expire
  retired_credential_purge: true
```

The worker's daily tick:

- sets `review_due_at` on approval and after each review; when it passes,
  the agent is flagged `review_overdue` on the Principals screen and in
  `/health.reviews_overdue`; policy may escalate to `restricted` after
  `review_grace_days`.
- restricts/disables on inactivity (`last_seen_at`), audited
  `principal.auto_restrict` / `principal.auto_disable`.
- expires `pending` registrations.

`POST /rest/v1/principals/{name}/review` (tier ≥4): records
`last_reviewed_*`, optionally narrows the grant, resets `review_due_at`.
The review screen shows the activity summary from `23 §3` beside the
grant so the reviewer decides with the evidence in front of them.

## 4. Restriction and restoration

`POST …/restrict {reason}`: stores the current grant in `restricted_from`,
sets `state='restricted'`, revokes OAuth refresh tokens (bearer tokens keep
working at tier 1 so the agent can still `whoami` and read its own
namespace — it can see *why* it was restricted via `memory_get agents/{name}/_status`
which the gateway writes).
`POST …/restore`: puts the stored grant back, sets `active`. Both governance-flagged.

## 5. Retirement

`POST …/retire` (tier ≥4; children must be retired first, or `?cascade=true`):
`state='retired'`, all credentials revoked and — under
`retired_credential_purge` — deleted; memory namespace `agents/{name}`
frozen read-only (its entries remain searchable to those who could read
them); jobs, audit rows, memory history all keep the name. Retirement is
the terminal state; `DELETE` (tier 5) is for mistakes, not for lifecycle.

## 6. Introspection for the agent

`whoami` and `GET /rest/v1/whoami` return, in addition to the grant:
`state`, `review_due_at`, `restricted_reason`, and `capabilities` — a
computed summary (`{"memory": ["org/**", …], "code": {"repos": […], "write": true}, "compute": {...}, "documents": {...}, "workspaces": n, "vault": {...}}`)
so an agent can describe what it can do without parsing the grant.

## 7. Guards (LIFE)

| id | Guard |
|---|---|
| LIFE-001 | only `active` authenticates at full grant |
| LIFE-002 | `restricted` is forced to tier 1 regardless of stored grant |
| LIFE-003 | `pending` has no credential until approved |
| LIFE-004 | `requested_grant` must narrow the code's template |
| LIFE-005 | registration code use count and expiry enforced |
| LIFE-006 | claim code single-use and 24 h |
| LIFE-007 | approve requires the parent in the approver's subtree |
| LIFE-008 | retire purges credentials, keeps history |
| LIFE-009 | inactivity restriction is audited |
| LIFE-010 | restore returns exactly the stored grant |
