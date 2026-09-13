# 07 — API surface: every new or changed endpoint and MCP method

This is the single list. Anything a later document adds must be added here
too. Existing endpoints not mentioned are unchanged. All REST responses use
the envelope in `datum_sync/errors.py`; OAuth endpoints use RFC 6749 shapes;
MCP uses JSON-RPC.

## 1. Principals (replaces `/rest/v1/accounts*`, which stay as aliases for one release)

| Method | Path | Tier | Body / notes |
|---|---|---|---|
| GET | `/rest/v1/principals?kind=&state=&parent=&q=` | 4 (tier 3 sees own subtree) | list; each row carries `effective_tier`, `state`, `kind`, `parent`, `children_count`, `live_sessions`, `review_due_at`, `flags[]` (`review_overdue`, `inactive`) |
| POST | `/rest/v1/principals` | 3 for `kind=agent` children of self; 4 otherwise | `{name, kind, parent?, max_tier, repo_scope, vault_scope?, proxy_grants?, federation_scope?, limits?, rate_limit_per_min?, metadata?, description?, state?: 'pending'}` — `narrows()` against the parent; 400 `GRANT_NOT_NARROWER` names the field |
| GET | `/rest/v1/principals/{name}` | self, sponsor, 4 | full row less secrets, `effective` (computed), `ancestors[]`, `children[]`, `tokens[]`, `sessions[]` |
| PATCH | `/rest/v1/principals/{name}` | per `03 §2` edit table | any authority field, `metadata`, `description`; **not** `is_admin`, `state`; 409 `DESCENDANTS_WOULD_WIDEN {names}` |
| POST | `…/{name}/approve` | 4, or sponsor | `{edits?}`; `pending → active` |
| POST | `…/{name}/reject` | 4, or sponsor | `{reason}` |
| POST | `…/{name}/restrict` | 4, or sponsor | `{reason}` |
| POST | `…/{name}/restore` | 4, or sponsor | — |
| POST | `…/{name}/disable` · `…/{name}/enable` | 4 | replaces `PATCH {disabled}` |
| POST | `…/{name}/retire?cascade=` | 4 | 409 `CHILDREN_ACTIVE {names}` without cascade |
| POST | `…/{name}/review` | 4, or sponsor | `{narrow?: edits, note}` → resets `review_due_at` |
| DELETE | `/rest/v1/principals/{name}` | 5 | as today |
| GET | `…/{name}/tokens` · POST · DELETE `…/tokens/{label}` | self (tier ≥3) or 4 | POST body gains `max_tier?` ≤ principal's tier; 400 `TOKEN_TIER_ABOVE_PRINCIPAL` |
| GET | `…/{name}/sessions` | self, sponsor, 4 | live and last-24h sessions |
| DELETE | `…/{name}/sessions/{id}` | self, sponsor, 4 | ends it (`revoked`) |
| GET | `…/{name}/activity?window=7d` | self, sponsor, 4 | rollup from `audit_log` (`08 §4`); counts only, no targets below tier 4 |
| POST | `…/{name}/passwd` | 4 (self for humans) | refused for `kind='agent'` (PRIN-007) |

`GET /rest/v1/whoami` returns the document in `03 §8`.

The `/rest/v1/accounts/{a}/agents*` routes map onto principals whose
`parent = a` and `kind = 'agent'`; `POST …/agents` creates a child with the
parent's scope (the old behaviour) and a baseline token; `…/agents/{n}/token`
mints label `agent`. Removed with migration 018.

## 2. Enrolment

| Method | Path | Auth | Notes |
|---|---|---|---|
| POST | `/rest/v1/enrolment/codes` | 3 (auto_approve needs 4) | `{template, max_uses?, expires_in_hours?, auto_approve?}` → `{code}` once. `template` must narrow the caller's own effective grant. |
| GET | `/rest/v1/enrolment/codes` · DELETE `…/{id}` | 3 own, 4 all | |
| POST | `/enrol` | **public**, rate-limited per code | `{code, name, metadata?, requested?}` → `{state, claim_code?, token?, whoami?}` |
| POST | `/enrol/claim` | public | `{claim_code}` → `{token, whoami}` once |
| GET | `/rest/v1/enrolment/pending` | 4, sponsor | pending principals with their requested tuple and metadata |

Both public paths are added to `PUBLIC_PATHS` in `api.py`; each writes an
anonymous audit row (`enrol.request`, `enrol.claim`) with the submitted name.

## 3. OAuth additions

| Method | Path | Notes |
|---|---|---|
| POST | `/oauth/device` | bearer required (`04 §3`) |
| POST | `/oauth/token` | `grant_type=urn:ietf:params:oauth:grant-type:device_code`, `device_code`, `client_id` |
| GET | `/oauth/authorize?…&on_behalf_of=` | `04 §4` |
| GET | `/.well-known/oauth-authorization-server` | adds `device_authorization_endpoint`, `scopes_supported`, `resource_indicators_supported: true` |
| GET | `/.well-known/oauth-protected-resource` | adds `scopes_supported` |
| GET | `/rest/v1/elevations?status=` | 4, sponsor | pending and recent device requests |
| POST | `/rest/v1/elevations/{id}/approve` | 4, sponsor | `{scope?}` narrower or equal |
| POST | `/rest/v1/elevations/{id}/deny` | 4, sponsor | `{reason}` |
| GET | `/rest/v1/auth/clients` | 4 | gains `pruned_last_run`, `unused` |

## 4. Federation

| Method | Path | Tier | Notes |
|---|---|---|---|
| POST/PATCH | `/rest/v1/connections` | 4 (5 for `allow_private_origin`) | `type: mcp` accepted; guards validated against the cached schema; response carries `warnings[]` |
| POST | `/rest/v1/connections/{name}/test` | 4 | for `mcp`: `initialize` + `tools/list`, returns `tool_count` and `guard_coverage {guarded, unguarded, denied_by_default}` |
| POST | `/rest/v1/connections/{name}/refresh` | 4 | force a catalogue refresh |
| GET | `/rest/v1/federation` | 4 | per connection: status, tool count, last refresh, clash list |
| GET | `/rest/v1/federation/{name}/tools` | 4 | cached tools with `guarded`, matching guards, and (for the caller) whether listed |
| GET | `/rest/v1/federation/profiles` | 4 | shipped profiles |
| GET | `/rest/v1/approvals?status=` | 4, sponsor | pending calls |
| POST | `/rest/v1/approvals/{id}/approve` · `/reject` | 4, sponsor | approve forwards under the snapshot and stores the result |

## 5. Review

| Method | Path | Tier | Notes |
|---|---|---|---|
| GET | `/rest/v1/review` | 4 (sponsor sees own subtree) | one queue: pending enrolments, pending elevations, pending calls, review-overdue, inactive-restricted, name clashes, stale upstreams. Each item: `{kind, age, principal?, link}` |
| GET | `/health` | public | adds `reviews_overdue`, `pending_approvals`, `federation {…}`, `sessions_live` |

## 6. MCP

Transport changes (`03 §5`): `initialize` returns `Mcp-Session-Id`; other
methods require it (agents) or tolerate its absence (human tokens, one
release); `DELETE /mcp` ends a session; `-32000 SESSION_LIMIT`.

`initialize` capabilities: `{"tools": {"listChanged": false}, "resources":
{"subscribe": false, "listChanged": false}}`. `serverInfo.name` stays
`datum-sync`.

| Method | Change |
|---|---|
| `tools/list` | tier filter; federated tools merged; every tool carries `annotations.datumMinTier` |
| `tools/call` | dispatch order: built-ins → workspace → federated (`05 §4.2`); job submission passes the principal |
| `resources/list` · `resources/templates/list` · `resources/read` | new: `datum://jobs/{id}`, `datum://jobs/{id}/log`, `datum://jobs/{id}/artifacts/{name}`, `datum://vault/{path}`, `datum://{connection}/{uri}` (federated, guarded) |

Built-in tools (shown only when usable; tier is the minimum):

| Tool | Tier | Behaviour |
|---|---|---|
| `whoami` | 1 | `03 §8` |
| `job_status(job_id)` · `job_result(job_id)` · `job_list(limit?)` | 2 | submitter, sponsor or tier 4; `job_result` returns the same content blocks a completed call would |
| `job_cancel(job_id)` | 3 | |
| `vault_read` · `vault_list` | 1 | as today |
| `vault_write` | 3 | as today, tier-checked |
| `proxy_request` | 3 | `check_proxy_access` no longer requires `kind=agent` |
| `session_info()` | 1 | own session, limit, other live sessions (ids and ages only) |
| `elevate(scope)` | 1 | convenience wrapper over `POST /oauth/device` for the gateway's own registered client: returns `user_code` and `verification_uri`; the agent tells its operator |
| `pending_status(id)` · `pending_result(id)` | 1 | `05 §5`, requester only |
| `delegate_create({name, narrowing, expires_in_seconds?})` | 3 | creates a child `agent` principal `active` under the caller with the given narrowing (must narrow), a baseline token with the given expiry (default 24 h, max `POLICY_MAX_DELEGATED_TOKEN_HOURS` 168), returns `{principal, token}` once |
| `delegate_revoke({name})` | 3 | own children only: `retire` |
| `<prefix>__<upstream>` | per guard | federated |

Error data on `TIER_REQUIRED` results carries `{required, effective,
elevate}` so a client can render the next step.

## 7. Error codes added

`GRANT_NOT_NARROWER`, `DESCENDANTS_WOULD_WIDEN`, `PRINCIPAL_PENDING`,
`PRINCIPAL_DISABLED`, `PRINCIPAL_RETIRED`, `PRINCIPAL_REJECTED`,
`ANCESTOR_DISABLED` (and `_RESTRICTED`, `_RETIRED`), `TIER_REQUIRED`,
`TOKEN_TIER_ABOVE_PRINCIPAL`, `SESSION_LIMIT`, `SESSION_REQUIRED`,
`JOB_LIMIT`, `CHILDREN_ACTIVE`, `ENROL_CODE_INVALID`, `ENROL_NAME_TAKEN`,
`CLAIM_INVALID`, `FEDERATION_DENIED`, `UPSTREAM_UNAVAILABLE`,
`PENDING_APPROVAL`, `invalid_scope` (OAuth), `authorization_pending`,
`slow_down`, `expired_token`, `access_denied` (OAuth device).
