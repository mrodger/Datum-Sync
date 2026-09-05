# 03 — Authority: Principals, Credentials, Grants, Tiers, Delegation

This is the load-bearing document. Every other component asks one question of
this one: *given this request, what is the effective grant, and does it permit
this verb on this target?*

## 1. Principals

```sql
principals (
  id            SERIAL PK,
  name          TEXT UNIQUE NOT NULL,
  kind          TEXT NOT NULL CHECK (kind IN ('human','agent','system')),
  parent_id     INTEGER NULL REFERENCES principals(id),
  grant         JSONB NOT NULL,          -- Grant document, §3
  description   TEXT,
  disabled      BOOLEAN NOT NULL DEFAULT false,
  created_by    INTEGER NULL REFERENCES principals(id),
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_seen_at  TIMESTAMPTZ
)
```

- `kind` is descriptive and drives defaults (a `human` may hold a password;
  an `agent` may not), never authority. Authority is the grant.
- `parent_id` forms the **delegation tree**. `NULL` means a root principal.
  Depth is bounded by `policy.max_delegation_depth` (default 4).
- `system` is reserved for the two built-in principals created by migration:
  `system:worker` (what the worker acts as when it submits scheduled jobs)
  and `system:local` (what the CLI acts as when run without `--as`).

## 2. Credentials

One table, one resolver.

```sql
credentials (
  id            BIGSERIAL PK,
  principal_id  INTEGER NOT NULL REFERENCES principals(id) ON DELETE CASCADE,
  kind          TEXT NOT NULL CHECK (kind IN
                  ('token','password','session','oauth_access','oauth_refresh')),
  secret_hash   TEXT NOT NULL,         -- sha256 hex for all but password; argon2 for password
  label         TEXT,                  -- human label for tokens ("laptop", "drone-dispatch")
  client_id     TEXT NULL REFERENCES oauth_clients(client_id) ON DELETE CASCADE,
  audience      TEXT,                  -- RFC 8707 resource, oauth_* only
  scope         TEXT,                  -- OAuth scope string, oauth_* only
  expires_at    TIMESTAMPTZ,
  revoked_at    TIMESTAMPTZ,
  rotated_to    BIGINT NULL REFERENCES credentials(id) ON DELETE SET NULL,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_used_at  TIMESTAMPTZ,
  UNIQUE (secret_hash)
)
```

Rules:

- **Hashing.** Token-shaped kinds are 32 random URL-safe bytes; store
  `sha256(raw)`. They have no dictionary to attack and sit on the hot path.
  Passwords use argon2id (`argon2-cffi` defaults) and are only ever verified
  at the consent screen and UI sign-in.
- **A principal may hold several tokens.** Each has a label and can be
  revoked independently. This replaces "one token per account, rotate to
  replace". Rotation is: mint new, revoke old, both visible in the UI.
- **At most one password per principal**, only for `kind='human'`. Setting
  a new one revokes the old row.
- **Sessions** are minted by UI sign-in, carried in an `HttpOnly`,
  `SameSite=Lax` cookie named `dg_session`, `Secure` when `PUBLIC_URL` is
  https. TTL `SESSION_TTL_SECONDS` (default 12h). Accepted only on
  `/rest/v1/`, `/ui/`, `/serve/`.
- **OAuth access/refresh** rows are minted by `12-oauth.md`. Refresh rotation
  writes `rotated_to`; presenting a rotated refresh token revokes the whole
  family (all rows for that `client_id` + `principal_id`).
- **The resolver never distinguishes "unknown" from "revoked" from "expired"
  in the status code** — all are 401 with an `error.code` that *does*
  distinguish them, because the client that holds a real-but-expired token
  needs to know to refresh, and an attacker with a random string learns
  nothing from `TOKEN_EXPIRED` it could not learn from timing.

## 3. The Grant document

Pydantic model `Grant` in `datumgate/models.py`. Stored in `principals.grant`
and, frozen, in `jobs.effective_grant`.

```json
{
  "tier": 3,
  "repositories": ["SCIMAC", "Testing"],
  "connections": {
    "use":   ["*"],
    "proxy": ["openai", "anthropic"]
  },
  "vault": {
    "read":       ["dev/**", "shared/**"],
    "write":      ["dev/**"],
    "quarantine": ["quarantine/research/**"],
    "promote":    ["quarantine/research/** -> shared/long_term/**"],
    "deny":       ["secrets/**", "private/**"]
  },
  "memory":    {"read": ["org/**"], "write": ["agents/datum-main/**"], "deny": []},
  "code":      {"connections": ["github"], "repos": ["datum/*"], "write": false, "tools": {"allow": ["*"], "deny": ["delete_*"]}},
  "compute":   {"connections": [], "hosts": [], "commands": {"allow": [], "deny": []}, "paths": {"read": [], "write": []}, "approval_required": []},
  "documents": {"connections": [], "folders": [], "write": false, "share": false},
  "mcp":       {"connections": {}},
  "limits": {
    "calls_per_minute": 120,
    "jobs_per_hour": 60,
    "concurrent_jobs": 2
  },
  "admin": false
}
```

Field semantics:

| Field | Type | Meaning |
|---|---|---|
| `tier` | int 1–5 | Verb ceiling, §4 |
| `repositories` | `["*"]` or list of names | Which repositories the principal can see, run and (tier ≥4) manage. `[]` = none. **Never null.** |
| `connections.use` | `["*"]` or names | Connections this principal may *publish* workspaces against (tier ≥4) — checked at publish against the publisher |
| `connections.proxy` | names only | Connections this principal may forward requests through via the credential proxy. `*` is refused here on purpose: proxy rights are enumerated. |
| `vault.read/write/quarantine` | glob lists | See `08-vault-gate.md` |
| `vault.promote` | list of `"src_glob -> dst_glob"` | Promotion edges, not two independent lists — an edge says *from where to where* |
| `vault.deny` | glob list | Wins over every allow |
| `memory` `code` `compute` `documents` `mcp` | blocks | The core company resources and other federated MCP servers — semantics and narrowing rules in `21-federation-and-core-resources.md §3–§4`. Omitted blocks mean no access. |
| `limits` | ints | Enforced per principal; `null` field = policy default |
| `admin` | bool | Shortcut for "tier ≥4"; **derived**, never set independently. Present for readability of stored documents. Validation rejects `admin: true` with `tier < 4`. |

Coherence rules (validated at every write, see `08-vault-gate.md §4` for the
vault ones):

1. `tier` in 1..5.
2. `admin == (tier >= 4)`.
3. `repositories`: either exactly `["*"]` or a list of well-formed names, no
   duplicates.
4. `connections.proxy` contains no `*`.
5. Every `vault.write` pattern is also matched by some `vault.read`
   pattern (write-implies-read, checked by *pattern subsumption*, §6).
6. Every `vault.promote` edge's source is covered by `vault.quarantine` or
   `vault.write`, and its destination is covered by `vault.write`.
7. No `vault.deny` pattern equals an allow pattern (dead allow).
8. `limits` values are positive or null.
9. A principal with `tier == 1` has no `write`, `quarantine`, `promote`, and
   an empty `connections.proxy`.
10. A principal with `tier <= 2` has no `write` and no `promote`.

## 4. Tiers

Tier sets the ceiling on *verbs*. The grant's other fields set the ceiling on
*targets*. Both must pass.

| Tier | Name | Verbs unlocked (cumulative) |
|---|---|---|
| 1 | Observer | authenticate; `whoami`; list repositories and workspaces in scope; read own audit rows; vault `read`/`list` within scope |
| 2 | Reader | read jobs, logs and artifacts in scope; MCP `resources/read`; use tier-≤2 connections through workspaces; vault `quarantine` writes |
| 3 | Operator | submit / cancel / resubmit jobs in scope; `/stream`, `/download`, `/upload`; MCP `tools/call`; vault `write`; credential proxy through granted connections at tier ≤3; create and edit schedules and automations in scope; request promotions |
| 4 | Admin | publish (`sync`) into scope; create/edit/disable principals **with tier ≤ 4 and grant ⊆ own**; manage connections up to tier 4; approve promotions; read all audit rows in scope; register proxied hosted services |
| 5 | Superuser | everything, all scopes; create tier-5 principals; key rotation; policy reload; delete (not just disable) principals |

Connection tier is the sensitivity of the credential, 1–5, same scale. A
principal may cause a connection to be used — by publishing a workspace that
declares it, or by proxying through it — only if `principal.tier >= connection.tier`.

Hosted services can require a minimum tier to view (`13-hosted-services.md`).

## 5. Delegation

A principal with `tier >= 3` may create child principals of `kind='agent'`
(tier ≥4 may create `human` too). The rule that makes delegation safe:

> **A child's grant MUST narrow its parent's grant** (`narrows(child, parent)`
> returns true), and the child's tier MUST be ≤ the parent's tier, and a
> tier-3 creator may only create tier ≤ 2 children.

`narrows(child, parent)` in `datumgate/authority/grants.py`:

- `child.tier <= parent.tier`
- `child.repositories ⊆ parent.repositories` (`["*"]` ⊆ only `["*"]`)
- `child.connections.use ⊆ parent.connections.use` (same rule for `*`)
- `child.connections.proxy ⊆ parent.connections.proxy`
- For each vault action `a` in `read, write, quarantine`: every pattern in
  `child.vault[a]` is **subsumed** by some pattern in `parent.vault[a]` (§6)
- Every `child.vault.promote` edge is subsumed source-wise and
  destination-wise by some parent edge
- `child.vault.deny ⊇ parent.vault.deny` (a child may deny more, never less;
  compared by subsumption in the other direction — every parent deny pattern
  must be subsumed by some child deny pattern)
- Every child limit ≤ parent limit (null = policy default, compared as such)

### 5.1 Effective grant

At authentication the resolver walks `parent_id` to the root and computes:

```
effective = grant(self)
for ancestor in ancestors (nearest first):
    effective = intersect(effective, grant(ancestor))
    if ancestor.disabled: raise 401 ANCESTOR_DISABLED
```

`intersect` is the pairwise meet: min tier, set intersection on names (with
`*` as identity), pattern intersection on vault globs (§6), union on deny,
min on limits. Because every child was checked to narrow its parent at write
time, `intersect` is normally a no-op; it exists so that **narrowing or
disabling a parent later takes effect on children immediately**, without
walking the tree to rewrite them.

The `Principal` object handed to request handlers carries `effective`, and
nothing downstream reads `principals.grant` directly.

### 5.2 Frozen grant on jobs

`jobs.effective_grant` is a copy of `principal.effective` at submit time. The
worker builds the child's connection set and the vault scope it enforces for
that run from the frozen copy. A drone dispatched with a narrow slice keeps
that slice even if its dispatcher is later widened; a job submitted by a
principal later disabled still runs to completion unless cancelled (the
cancel path is available to any admin in scope).

### 5.3 Who may edit a grant

| Actor tier | May set on target |
|---|---|
| 5 | anything |
| 4 | any principal whose effective grant ⊆ actor's effective grant, to any new grant ⊆ actor's effective grant, tier ≤ 4 |
| 3 | own children only, to any grant narrowing the actor's own |
| ≤2 | nothing |

Editing a grant re-validates every descendant with `narrows()` and refuses
the edit if any descendant would no longer narrow — the error names the
descendants. (The alternative, silently narrowing them via `intersect`,
would hide the change from the person making it.)

## 6. Glob subsumption

Vault patterns use the semantics in `08-vault-gate.md §2` (`*` is one
segment, `**` is any depth). Delegation needs `subsumes(outer, inner)`:
true iff every path matched by `inner` is matched by `outer`.

Exact subsumption over this glob language is decidable and small. Implement
by segment-wise comparison:

```
subsumes(outer, inner):
  O = outer.split('/'), I = inner.split('/')
  return walk(O, 0, I, 0)

walk(O, i, I, j):
  if i == len(O) and j == len(I): return True
  if i == len(O): return False
  if O[i] == '**':
      # '**' absorbs zero or more inner segments
      for k in range(j, len(I)+1):
          if walk(O, i+1, I, k): return True
      return False
  if j == len(I): return False
  if I[j] == '**': return False          # inner is broader here
  if O[i] == '*' or O[i] == I[j]: return walk(O, i+1, I, j+1)
  if O[i] contains '?' : compare literally against a non-wild inner segment
  return False
```

`intersect_patterns(a, b)` for the effective grant computation returns the
list `[p for p in a if any(subsumes(q, p) for q in b)] + [q for q in b if any(subsumes(p, q) for p in a)]`, deduplicated. This is conservative (it may
drop patterns that overlap without either subsuming the other), which is
the correct direction for an authority computation.

## 7. Resolution algorithm (request → Principal)

```
resolve(request):
  raw = bearer_token(request)
  if raw:
      cred = SELECT ... FROM credentials WHERE secret_hash = sha256(raw)
             AND kind IN ('token','oauth_access')
      if not cred: 401 UNAUTHENTICATED
      check revoked_at / expires_at → 401 TOKEN_REVOKED / TOKEN_EXPIRED
      if cred.kind == 'oauth_access' and canonical(cred.audience) != canonical(MCP_RESOURCE):
          401 WRONG_AUDIENCE
  elif path allows cookie and cookie 'dg_session' present:
      cred = SELECT ... WHERE secret_hash = sha256(cookie) AND kind='session'
      same checks
  else: 401 UNAUTHENTICATED with WWW-Authenticate: Bearer resource_metadata="..."

  p = principals[cred.principal_id]
  if p.disabled: 401 PRINCIPAL_DISABLED
  effective = compute_effective(p)          # §5.1, walks ancestors
  rate-limit check on effective.limits.calls_per_minute → 429 RATE_LIMITED
  UPDATE credentials SET last_used_at=now(); UPDATE principals SET last_seen_at=now()
  return Principal(id, name, kind, effective, credential_kind, client_id, trace_id)
```

A bearer token wins over a cookie when both are present.

`DATUM_GATE_AUTH=off` (development only) short-circuits to a synthetic
tier-5 principal named `auth-disabled`, and is refused unless the server is
bound to loopback **and** the connecting socket is loopback — both checks,
because `uvicorn --host` bypasses the config value.

## 8. Rate limits

`limits.calls_per_minute` is enforced in the resolver with a sliding window
keyed by principal id, in memory per process, **and** recorded to a
`rate_windows(principal_id, window_start, count)` table only when a process
count > 1 is configured (`WORKER_COUNT`/`API_COUNT` > 1 in config). One
process: memory. Several: the table, with `INSERT … ON CONFLICT DO UPDATE`.

`limits.jobs_per_hour` and `limits.concurrent_jobs` are enforced in
`jobs.submit` by counting rows — always the database, never memory.

## 9. Password lockout

Unchanged in spirit from the original and kept because it is right:

- Keyed on the **submitted principal name**, not the client address.
- Checked **before** the database is consulted, so unknown and known names
  behave identically.
- 5 failures in 15 minutes → 429 with `Retry-After`.
- A semaphore (`PASSWORD_MAX_CONCURRENT`, default 4) bounds concurrent
  argon2 verifications; an unknown name still costs a full hash.
- Lives in `passwords.authenticate()`, so no second caller can omit it.

## 10. The `Principal` object

```python
@dataclass(frozen=True)
class Principal:
    id: int
    name: str
    kind: str                       # human | agent | system
    effective: Grant                # intersected, §5.1
    credential_kind: str            # token | session | oauth_access | dev
    credential_id: int | None
    client_id: str | None           # OAuth client, if any
    trace_id: uuid.UUID             # minted per request
    client_trace_id: str | None     # X-Trace-Id header, untrusted

    # Convenience predicates — every one delegates to grants.py so there is
    # exactly one implementation of each rule.
    def allows_repo(self, name) -> bool
    def requires_tier(self, n) -> None            # raises 403 TIER_DENIED
    def allows_vault(self, action, path) -> bool
    def may_proxy(self, connection_name, connection_tier) -> bool
    def may_use_connection(self, name, tier) -> bool
```

Every 403 raised from these predicates carries `detail` naming the verb,
the target, and the tier or scope that refused it. That detail is safe to
return: the caller already knows what it asked for.
