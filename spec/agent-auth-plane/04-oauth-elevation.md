# 04 — OAuth: scopes that mean something, and elevation for headless agents

`oauth.py` is kept as-is. This document adds a scope vocabulary the resolver
enforces, a device-authorisation grant for agents that have no browser, and
a consent page that can bind a grant to an agent principal.

## 1. What OAuth is for here

An OAuth access token is the **elevated** credential (`02 §3`). It differs
from a bearer token in three ways the code must enforce, not merely record:

1. it is short-lived and rotates;
2. it carries a **scope** that sets a tier ceiling;
3. minting it involved a person — at the consent page, or at the Approvals
   screen for a device-flow request.

## 2. Scope vocabulary

| Scope | Tier ceiling | Unlocks (from `03 §6`) |
|---|---|---|
| `mcp` | 2 | read-only use: `whoami`, job status/result, vault reads, tier-≤2 workspaces |
| `mcp:operate` | 3 | submit jobs, vault write, proxy, schedules, federated writes |
| `mcp:admin` | 4 | principal and connection management from an MCP client |

Rules:

- Scopes are space-separated per RFC 6749. Unknown scopes → `invalid_scope`.
  An absent scope means `mcp`.
- `scope_tier(scope)` is the maximum ceiling among the scopes present.
  `Principal.effective_tier = min(max_tier, token_tier_cap, scope_tier)`. A
  scope can never raise a principal above its own `max_tier`; the consent
  page shows the *effective* result, not the requested one.
- The `oauth-protected-resource` document advertises
  `scopes_supported: ["mcp", "mcp:operate", "mcp:admin"]` so MCP clients that
  read it ask for the right thing.
- Refresh preserves scope; it cannot widen it (`_rotate_refresh` already
  copies `row["scope"]`).

Guard `ELEV-001`: an access token with scope `mcp` cannot submit a job even
when the principal is tier 5.

## 3. Device authorisation grant (RFC 8628)

For agents with no browser and no operator at a keyboard at the moment of
elevation.

| Endpoint | Purpose |
|---|---|
| `POST /oauth/device` | `{client_id, scope, resource}` → `{device_code, user_code, verification_uri, verification_uri_complete, expires_in: 600, interval: 5}`. **Requires a bearer token**: the requesting principal is recorded, and the elevation is for *that* principal. An unauthenticated device request would let anyone create approval noise for anyone. |
| `POST /oauth/token` `grant_type=urn:ietf:params:oauth:grant-type:device_code` | polls; `authorization_pending`, `slow_down`, `expired_token`, `access_denied`, or the token response |
| UI Approvals screen / `POST /rest/v1/elevations/{id}/approve|deny` | a person decides |

Table `oauth_device_codes(id, device_code_hash, user_code, client_id,
principal_id, scope, resource, requested_at, expires_at, decided_at,
decided_by, decision CHECK IN ('approved','denied'), consumed_at, last_polled_at)`.

- `user_code` is 8 characters from an unambiguous alphabet, shown in the UI
  beside the principal's name, kind, sponsor, requested scope, and what
  that scope *would* unlock for this principal (the `elevate` block from
  `whoami`).
- Who may approve: tier ≥4, **or the requesting principal's sponsor when the
  requested ceiling ≤ the sponsor's own effective tier**. This keeps a
  tier-3 operator able to elevate their own agent to tier 3 without an admin.
- A poll faster than `interval` → `slow_down`, and the interval is raised by
  5 s for that code. A device code is single-use; the second successful
  poll → `invalid_grant` and the family is revoked (same `_Reuse` path).
- Policy `POLICY_ELEVATION_AUTO_APPROVE_SCOPES` (default `["mcp"]`) lists
  scopes that need no decision: a `mcp` request from an `active` principal
  is approved on creation. Everything above needs a person.
- The minted tokens are ordinary `oauth_tokens` rows: `client_id` the
  registered client, `account_id` the **agent** principal, `scope` as
  approved (an approver may narrow it, never widen it — guard `ELEV-003`).

The audit rows: `oauth.device.request`, `oauth.device.approve|deny`
(governance), `oauth.device.consume`.

## 3.1 Clients that cannot drive the device flow

Claude Code and Codex elevate only through their built-in PKCE login
(`12 §1`). For them the consent page is the elevation step: §4's scope
picker lets the human choose `mcp:operate` there, and `on_behalf_of` binds
the token to the agent. The device flow is for clients with no browser at
all (the Datum-3.0 bridge, scripts). Both paths mint the same
`oauth_tokens` row and are indistinguishable downstream.

## 4. Consent page binding

Today the consent page signs in a human and binds the grant to that human's
account. Two additions:

- The `scope` field is shown as a plain-language list of what it unlocks
  for **this** account, with the effective ceiling after the account's own
  tier is applied. When the request carries no scope, or when
  `POLICY_CONSENT_SCOPE_PICKER` is on (default), the page offers a
  **scope picker** defaulting to the narrowest scope the principal can use
  and never listing one above its ceiling (guard `ELEV-011`). The chosen
  value is what `oauth_codes.scope` records. The page MUST NOT say "gains no
  permissions of its own" once scopes are enforced.
- Registration (`POST /oauth/register`) accepts a body with no `scope` and
  a `client_name` containing spaces or slashes; scope is validated at
  authorize, never at register (guard `ELEV-012`). Codex registers without
  scopes and requests them later (`12 §3.4`).
- An optional `on_behalf_of=<agent name>` query parameter (also a form
  field) lets an operator authorise a client for one of their agents. The
  signed-in human must be the agent's sponsor or tier ≥4, and the requested
  scope ceiling must not exceed what the human could set on the agent
  (`03 §2`, edit rules). The code and tokens bind to the agent principal;
  the audit row names both (`detail.on_behalf_of`). Guard `ELEV-004`.

CSRF: the existing reasoning ("the credential is submitted in this same
request") still holds for the human path. It does not hold for
`on_behalf_of` if the page ever gains a remembered login, so the form gets a
per-render nonce in a `SameSite=Strict` cookie now, while it is cheap.

## 5. Client registration hygiene

- `POST /oauth/register` is rate-limited by client address to 30/hour, with
  `X-Forwarded-For` **ignored** (the tunnel presents one address; a global
  limit is acceptable because registration is rare).
- The worker's daily tick deletes `oauth_clients` with no `oauth_tokens`
  and no `oauth_device_codes` referencing them and `created_at` older than
  `RETENTION_UNUSED_OAUTH_CLIENT_DAYS` (30). `ON DELETE CASCADE` on the
  code/token tables is already declared.
- The Auth Services screen shows `total` and `pruned_last_run`.

## 6. Token introspection — not built

RFC 7662 introspection would let a federated upstream verify a gateway
token. No upstream needs that in phase 1: the gateway is the *client* of
every upstream, with its own credential. Recorded in `11 D-11`.

## 7. Guards

| id | Guard | Test must fail when |
|---|---|---|
| ELEV-001 | scope caps effective tier | `scope_tier` removed from resolve |
| ELEV-002 | unknown scope → `invalid_scope` | vocabulary check removed |
| ELEV-003 | approver may narrow scope, never widen | comparison removed from approve |
| ELEV-004 | `on_behalf_of` requires sponsor or tier 4 | subtree check removed |
| ELEV-005 | device request requires a bearer token | auth dependency removed from `/oauth/device` |
| ELEV-006 | device code single-use; reuse revokes family | `consumed_at` check removed |
| ELEV-007 | non-auto scopes need a decision before the token mints | `decision = 'approved'` check removed from the poll |
| ELEV-008 | `slow_down` on fast polling | interval check removed |
| ELEV-009 | refresh cannot widen scope | scope copy replaced by request value |
| ELEV-010 | unused clients pruned; referenced ones kept | `NOT EXISTS` clause removed |
| ELEV-011 | scope picker never offers a scope above the ceiling | filter removed |
| ELEV-012 | register accepts no-scope bodies; authorize still validates | check moved |
