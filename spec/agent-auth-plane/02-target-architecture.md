# 02 — Target architecture

## 1. The product in one paragraph

Datum-Sync becomes the **authority server and MCP gateway** for an
organisation's people and agents. Every caller is a principal with a grant.
An agent is enrolled once, approved by a person, and thereafter holds a
long-lived **baseline** credential that lets it run a narrow, single-session
configuration on its own. When it needs more — company code, machines,
documents, production connections, more concurrency — it connects to the
gateway's MCP endpoint and **elevates** through OAuth. Elevation is
short-lived, scoped, bound to the agent's own principal, and approved by a
human when policy says so. Everything it then reaches — published
workspaces, the vault, the credential proxy, and federated upstream MCP
servers — is filtered through one effective grant and recorded in one audit
spine under one trace id. The gateway contains no model.

## 2. Components

```
                         people                                    agents
        ┌───────────────┬──────────────┐        ┌──────────────┬──────────────┬─────────────┐
        │ browser (UI)  │ Claude.ai /  │        │ Claude Code  │ OpenClaw /   │ research    │
        │               │ ChatGPT /    │        │ (MCP client) │ Hermes       │ drone       │
        │               │ Copilot      │        │              │ (MCP client) │ (child)     │
        └──────┬────────┴──────┬───────┘        └──────┬───────┴──────┬───────┴──────┬──────┘
               │ cookie        │ OAuth PKCE            │ bearer + device-flow OAuth   │ bearer
               ▼               ▼                       ▼              ▼               ▼
┌──────────────────────────────────────────────────────────────────────────────────────────────┐
│ EDGE  (api.py middleware, outermost → innermost)                                              │
│   trace_and_audit ─► authenticate ─► session (Mcp-Session-Id lease)  ─► rate_limit           │
├──────────────────────────────────────────────────────────────────────────────────────────────┤
│ AUTHORITY  (auth.py, tokens.py, principals.py [new], grants.py [new], lifecycle.py [new])     │
│   credentials ──► principal row ──► effective grant = own ∩ every ancestor                    │
│   lifecycle state · tier ceiling · token ceiling · OAuth scope ceiling · limits               │
├──────────────────────────────────────────────────────────────────────────────────────────────┤
│ SURFACES                                                                                     │
│   /rest/v1  (api.py)   /mcp (mcp.py)   /oauth + /.well-known (oauth.py, device.py [new])      │
│   /ui + /ui/v2 (ui.py)   /serve (services.py)   /stream /download /upload                    │
├──────────────────────────────────────────────────────────────────────────────────────────────┤
│ PROVIDERS  — every one projects into tools/list under the caller's effective grant           │
│   workspaces + jobs      vault gate        credential proxy (http)      FEDERATION [new]      │
│   (publish, worker)      (vault_fs)        (proxy.py)                   upstream MCP servers  │
│                                                                         as `mcp` connections │
├──────────────────────────────────────────────────────────────────────────────────────────────┤
│ AUDIT SPINE  audit_log (trace_id server-minted) · sessions · pending approvals · rollups      │
└──────────────────────────────────────────────────────────────────────────────────────────────┘
                                          │
              ┌───────────────────────────┼─────────────────────────────┐
              ▼                           ▼                             ▼
     PostgreSQL 16 + PostGIS       NFS vault (/vault)          upstream MCP servers
     (one DB, system of record)    (scoped reads/writes)       github · ssh/vm · gdrive · internal
```

Nothing new is a process. Federation is a client inside the API process;
sessions are rows; approvals are rows; the worker's existing tick gains
lifecycle housekeeping. One API process, one worker, one database, as today.

## 3. Two access levels for an agent

This is the mechanism that delivers "stripped down on its own, broader
services through MCP + OAuth". It uses credentials the code already has and
adds a ceiling to each.

| | **Baseline** | **Elevated** |
|---|---|---|
| Credential | an `account_tokens` row minted for the agent principal, label e.g. `baseline`, with `max_tier` cap (default 2) | an `oauth_tokens` access token bound to the agent principal, minted by the device flow (`04 §3`) or PKCE, carrying a scope |
| Lifetime | long-lived, revocable by label | `ACCESS_TOKEN_TTL_SECONDS` (1 h), refresh rotated; family revoked on reuse |
| Effective tier | `min(principal.max_tier, token.max_tier)` | `min(principal.max_tier, scope_tier(scope))` |
| Sessions | `limits.concurrent_sessions` default **1**; a reconnect on the same credential supersedes rather than collides (`12 §2`) | `limits.concurrent_sessions` default 4, per policy |
| Tools visible | `whoami`, `job_status/result`, tier-1/2 vault reads in scope, `memory_*` own namespace (if memory is built), workspaces in scope that need tier ≤ 2 | everything the effective grant reaches: submit, vault write, proxy, federated code/compute/documents, delegation |
| Human in the loop | once, at enrolment approval | at every elevation: the consent page (Claude Code, Codex) or the Approvals screen (device flow) for `mcp:operate` and above |
| Where enforced | `auth.resolve` computes the ceiling; `require_tier()` refuses verbs; session middleware refuses the second session | same functions — elevation only changes the inputs |

The rule that makes this real, and that `01 §4.1` shows is missing today:
**every verb has a tier, and one function checks it** (`03 §6`). Without that,
a ceiling is a number on a row.

## 4. Trust boundaries

| Boundary | What crosses it | Enforced by |
|---|---|---|
| Network → edge | credentials, JSON-RPC, forms | `authenticate` middleware: no credential, no route; loopback-only dev mode |
| Edge → authority | a raw token or cookie | `auth.resolve`: hash lookup, revoked/expired/state checks, audience binding |
| Authority → providers | a `Principal` carrying an *effective* grant | `require_tier`, `require_repo`, `vault.permits`, `check_proxy_access`, `federation.guards.check` |
| Gateway → upstream MCP server | a forwarded `tools/call` with injected auth | SSRF guard, argument guards, size/time caps, `Authorization` never forwarded from the caller |
| Gateway → workspace subprocess | resolved connection objects, params, frozen grant snapshot | `child.py` env strip; the subprocess is *not* sandboxed against the vault and this spec does not claim it is |
| Any → audit | identifiers only, never content | `audit.write` bounded detail; guards `AUDIT-*` |

## 5. Request flows

### 5.1 Enrolment (agent gets a principal)

```
sponsor (tier ≥3, UI)          gateway                              new agent
  POST /rest/v1/enrolment/codes ──► registration_codes row
        {template grant, max_uses, expires, auto_approve:false}
  ◄── code (shown once)
  ─── hands code to the agent's operator / config ───────────────────────►
                                                                    POST /enrol {code, name, metadata, requested_grant?}
                                 validate code; requested ⊆ template;
                                 INSERT principal kind=agent state=pending parent=sponsor
                                 ◄── {claim_code, state: pending}
  Review screen shows pending
  POST …/principals/{name}/approve {grant edits} ──► state=active; review_due_at set
                                                                    POST /enrol/claim {claim_code}
                                                                    ◄── {token (baseline, tier≤2), whoami}
```

With `auto_approve: true` the principal is created `active` and the token is
returned in the enrol response. Only tier-4 sponsors may mint auto-approve
codes.

### 5.2 Baseline MCP call under a session lease

```
agent                                        gateway
POST /mcp initialize  (Authorization: Bearer <baseline>)
                                             resolve → Principal(tier ceiling 2, sessions 1)
                                             mcp_sessions: count live for principal
                                               ≥ limit → 409 SESSION_LIMIT {active_session_id, started_at, idle_s}
                                               else INSERT session; reply with Mcp-Session-Id
POST /mcp tools/list  (Mcp-Session-Id: …)
                                             catalogue filtered: tier ≤ 2 tools only
POST /mcp tools/call vault_write
                                             require_tier(3) → isError "TIER_REQUIRED: elevate to mcp:operate"
DELETE /mcp (Mcp-Session-Id)                 session closed; or expires after SESSION_IDLE_SECONDS
```

### 5.3 Elevation by device flow (headless agent, human approves)

```
agent                                   gateway                                 human (UI)
POST /oauth/device {client_id, scope:"mcp:operate", resource}
◄── {device_code, user_code, verification_uri, interval}
                                        pending_elevations row (agent principal, scope)
                                                                                Approvals screen: "datum-main asks for mcp:operate for 1h"
                                                                                approve (tier ≥4, or the agent's sponsor for scope ≤ own)
POST /oauth/token grant_type=device_code … (polls)
◄── authorization_pending … ◄── {access_token, refresh_token, scope}
                                        oauth_tokens row bound to the AGENT principal
POST /mcp initialize (Bearer <elevated>)  → new session, tier ceiling 3, sessions 4
```

An agent that already has a browser-capable operator can use the existing
PKCE flow instead; the consent page then binds the grant to the principal
that signs in, which for an agent means the operator authorises **on the
agent's behalf** by naming it (`04 §4`).

### 5.4 Federated tool call with argument guards

```
agent: tools/call gh__push_files {owner:"datum", repo:"gateway", files:[…]}
  mapping = federated_tools[name] → (connection "github", upstream "push_files")
  principal.federation.code.connections ∋ "github"        else -32602 unknown tool
  guards for push_files:
     args: repos ← join(owner,"/",repo) = "datum/gateway"  ⊆ code.repos ["datum/*"]  ✓
     requires: write:true                                  code.write == true         ✓
  audit federate.call (target github:push_files, guarded values only) BEFORE forwarding
  forward with injected upstream auth, timeout, 1 MiB cap
  audit outcome; return content blocks verbatim
```

### 5.5 Approval-gated call

```
agent: tools/call vm__restart_service {host:"vm102", service:"datum-sync-api"}
  guard requires approval "restart" → INSERT pending_calls (args stored, governance)
  ◄── {status:"pending_approval", pending_id, label}
human approves in Approvals screen → gateway forwards under the REQUESTER's frozen grant → result stored
agent: pending_result(pending_id) → the upstream result
```

## 6. What is deliberately not in this architecture

- **No LLM, no intent evaluation.** Anomaly signals are deterministic rules on rollups.
- **No second process.** Federation caches, session counting and rate windows are designed for one API process; `11 D-09`.
- **No memory provider in phase 1.** The datum-gate memory design is sound but is a separate provider; it attaches at `05 §7` when wanted.
- **No teams in phase 1.** The delegation tree (`parent_id`) gives per-sponsor fencing now; teams are a labelling layer on top, scheduled last (`09 WP8`).
- **No full MCP relay.** Prompts, sampling, notifications and server-initiated streams are not federated. Tools and resources are.
- **No sandbox claim for workspaces.** A workspace subprocess runs with the worker's filesystem authority. The frozen grant on a job governs connections and attribution, not the child's syscalls.
