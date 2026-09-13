# 12 — Client harnesses: Claude Code, Codex, Datum-3.0

The gateway is harness-agnostic: it speaks MCP Streamable HTTP with bearer
or OAuth credentials and nothing else. This document records what each of
the three proposed clients can actually do on that wire, measured from
their documentation and source in September 2026, and what the gateway must
therefore accept so that enrolment, baseline use and elevation work from
all three without a client-specific branch anywhere in `datum_sync/`.

The rule this document enforces: **every client capability the gateway
relies on is in the MCP or OAuth specifications, and every client
limitation is absorbed by a gateway default, never by a client-specific
code path.** Where a client cannot do something (device flow, dynamic
registration with scopes), the gateway offers a standard alternative that
the other two clients can also use.

## 1. Capability matrix

| Capability | Claude Code | Codex CLI | Datum-3.0 (`datum_mcp/mcp_bridge.py`) | Gateway consequence |
|---|---|---|---|---|
| Transport | Streamable HTTP (`--transport http`), falls back to SSE; `type: "streamable-http"` accepted in JSON | Streamable HTTP (`url =`) | Streamable HTTP via the Python `mcp` SDK `streamablehttp_client`; stdio for local servers | `POST /mcp` only; `GET /mcp` stays 405. No SSE fallback is offered and none is needed: all three speak the HTTP transport. |
| Static bearer | `--header "Authorization: Bearer …"`, `headers` in JSON with `${VAR}` expansion, or `headersHelper` (a command run fresh per connection, re-run once on 401/403) | `bearer_token_env_var`, `http_headers`, `env_http_headers` | `DATUM_MCP_AUTH_TOKEN` env → `Authorization: Bearer` on an `httpx` client | Baseline tokens work everywhere. The claim response (`07 §2`) prints the token once and the three config snippets (§4). |
| OAuth (interactive) | Dynamic client registration by default; PKCE; browser or `claude mcp login <name> --no-browser` (prints the URL, accepts the pasted redirect); `oauth.scopes` pins requested scopes; `--client-id`/`--client-secret`/`--callback-port` for pre-registered clients; callback `http://localhost:PORT/callback` | `codex mcp login <name>`; DCR by default, `--oauth-client-registration dcr|cimd`; known to register **without scopes** and then request them (openai/codex #20503, #23242); fixed callback via `mcp_oauth_callback_port` | none built. The bridge has no OAuth client; `datum_mcp/oauth.py` is a *server* for claude.ai | PKCE + DCR (already built) is the common path. The gateway MUST tolerate a client whose registration carried no `scope` and whose authorize request does (`oauth.register` already ignores scope; `_check_resource` unaffected). Consent page gains a scope picker (§3.2). |
| Device flow (RFC 8628) | **not supported** as an MCP client | **not supported** | trivially implementable in the bridge (two HTTP calls) | Device flow stays (`04 §3`) for Datum-3.0 and scripts, but it is **not the only elevation path**: Claude Code and Codex elevate by an ordinary PKCE login whose consent page carries the scope (§3). |
| `Mcp-Session-Id` | handled by the client; reconnects with exponential backoff on drop (new `initialize`, old session not closed) | handled by the client | handled by the SDK; **a new session per `tools/call`** for HTTP servers (`call_tool` opens `streamablehttp_client` per call); SDK sends `DELETE` on exit | The session limit MUST supersede, not refuse, a re-`initialize` on the **same credential** (§2). Otherwise a Claude Code reconnect or a Datum-3.0 call after a crashed one is locked out for `SESSION_IDLE_SECONDS`. |
| Tool name constraints | 1–64 chars, `[A-Za-z0-9_.-]`; tools failing this are dropped and the model is told | not documented; assume the same | prefixes `mcp__<server>__`; OpenAI function names are limited to 64 chars | **Gateway tool names are ≤ 48 characters** so that a client prefix (`mcp__datum-sync__`, 17 chars) still fits in 64. `mcp.tool_name` and `federation.tool_name` truncate with a stable 6-char hash suffix on collision. Changes `03`/`05`, which said 128. |
| Input schema | JSON Schema 2020-12 validated; root `anyOf/oneOf/allOf` rewritten; property names 1–64 `[A-Za-z0-9_.-]` | as SDK | passed through to OpenAI function schema | Workspace `inputSchema` already conforms. Federated `input_schema` is passed through verbatim; a guard-validation warning is raised for property names outside the character set so the operator knows Claude Code will drop the tool. |
| Result size | 25,000 tokens default (`MAX_MCP_OUTPUT_TOKENS`), per-tool `_meta.anthropic/maxResultSizeChars` | not documented | strings concatenated into the model turn | `MAX_INLINE_BYTES` (64 KiB) already below the default. Federated results are capped at 1 MiB and truncated with a trailer. |
| Long calls | per-request timer max(60 s, tool timeout); calls > 2 min auto-backgrounded | `tool_timeout_sec` | SDK default | `_wait_seconds` and the job handle pattern (`07 §6`, `job_status/job_result`) mean no call needs to exceed 60 s. |
| Discovery cache | caches `tools/list` per server, "connects on first use" | — | snapshots `tools/list` every 300 s | A tier change or approval does not appear until the client reconnects or refreshes. `whoami` and the `TIER_REQUIRED.elevate` hint tell the agent to do so. |
| Runs headless | `claude -p` / Agent SDK: no `/mcp` panel; sign-in must be done from an interactive session first | CLI is interactive; `codex exec` headless | fully headless (a FastAPI service with an OpenRouter loop, or spawning `claude` CLI) | Enrolment and the baseline token need no browser. Elevation for a headless Claude Code or Codex needs an operator at a terminal once per refresh-token lifetime (30 days). |

Datum-3.0 additionally runs Claude Code as a subprocess (`server.py`
`run_cc`, `claude --output-format stream-json`), which inherits
`~/.claude.json`. So Datum-3.0 reaches the gateway two ways: through its own
bridge under the OpenRouter loop, and through Claude Code's client when the
`anthropic` engine is selected. Both must be enrolled as principals; they
are different processes with different credentials, and the audit spine
should show which one acted. The recommended shape is one sponsor account
(the operator) with two agent children, `datum3-bridge` and `datum3-claude`.

## 2. Session supersession

Addition to `03 §5`:

> On `initialize` at or over `limits.concurrent_sessions`, if a live session
> exists for the **same credential row** (same `account_tokens.id` or
> `oauth_tokens.id`) the oldest such session is ended with
> `end_reason = 'superseded'` and the new session is admitted. Only when
> every live session belongs to a *different* credential is `SESSION_LIMIT`
> returned.

"One session at a time" therefore means one live conversation per
credential, which is what an operator means by it; a client that crashed
and came back is the same conversation. Two different credentials (two
Codex instances each with the agent's token pasted in) still collide, which
is the case the limit exists for. Guard `SESS-005`: superseding a session
that belongs to a different credential must not happen.

Datum-3.0's per-call session pattern means its sessions are one call long;
the SDK's `DELETE` on exit closes them, and a crashed call is superseded by
the next. Its idle window should be short: `mcp_sessions` rows from a
credential whose `client_name` is `datum-3.0` may be ended after
`SESSION_IDLE_SECONDS_SHORT` (60 s) — a per-principal `limits.session_idle_seconds`
rather than a client-name branch.

## 3. Elevation without device flow

### 3.1 The problem

Claude Code and Codex elevate only by their built-in OAuth login. Both do
dynamic registration and PKCE against the gateway's existing endpoints.
Neither can drive `POST /oauth/device`. Codex may register with no scopes
and request them later; Claude Code pins scopes through `oauth.scopes` and
otherwise sends whatever `scopes_supported` advertises.

### 3.2 The consent page carries the scope choice

`04 §4` is extended: when the authorize request names **no** scope, or when
`POLICY_CONSENT_SCOPE_PICKER` is on (default on), the consent page shows a
scope selector defaulting to the narrowest scope that the signed-in
principal can use, listing for each option what it unlocks and the
effective tier after the principal's own ceiling. The chosen scope is
written to `oauth_codes.scope` and flows to the token as today.

This makes the same PKCE login serve as *baseline* (choose `mcp`) or
*elevation* (choose `mcp:operate`), from any client, with the human
decision in the same place — the consent page — regardless of which
harness opened the browser. The device flow remains for clients with no
browser at all (Datum-3.0's bridge, scripts).

### 3.3 Who is the principal

A Claude Code or Codex login signs in as a **human** at the consent page.
For an agent to hold an elevated OAuth token in one of those clients, the
operator uses `on_behalf_of=<agent>` (`04 §4`): the login URL
`claude mcp login` prints is opened, the operator appends
`&on_behalf_of=datum3-claude` (the consent page also offers an "authorise
for one of my agents" selector, so editing the URL is not required), signs
in as themselves, and the token binds to the agent. Claude Code then stores
that token under the server entry; from the gateway's side the caller is
the agent, at the approved scope, for one refresh-token lifetime.

For Datum-3.0's bridge, the bridge implements device flow: `POST
/oauth/device` with its baseline token, show `user_code` in the Datum UI,
poll `/oauth/token`, swap the bearer on its `httpx` client. That is about
forty lines and is recorded as a Datum-3.0 task, not a gateway one.

### 3.4 Registration compatibility

- `POST /oauth/register` MUST accept a registration with no `scope` field
  and no `grant_types` (already does), and MUST accept `client_name` values
  containing spaces and slashes (Codex sends its version string).
- The gateway SHOULD support **client ID metadata documents** (CIMD: a
  `client_id` that is an `https://` URL resolving to a JSON document of
  client metadata), which Codex can use with `--oauth-client-registration
  cimd` and which avoids the unbounded `oauth_clients` growth from DCR
  (`01 §4.8`). Phase 2; `11 D-25`.
- Loopback redirect URIs: both CLIs register the exact port they will
  listen on, so exact matching (`oauth._redirect_uri`) stands. No wildcard
  port allowance is added.

## 4. Onboarding snippets

The claim response and the Principal detail screen's **Connect** panel
render these, with the token and URL filled in. They are templates in
`datum_sync/static-v2/connect-snippets.js`, data not code.

**Claude Code — baseline token**

```bash
claude mcp add --transport http datum-sync https://sync.example.com/mcp \
  --header "Authorization: Bearer ${DATUM_SYNC_TOKEN}"
```

**Claude Code — elevated (OAuth), scope pinned**

```bash
claude mcp add-json datum-sync \
  '{"type":"http","url":"https://sync.example.com/mcp","oauth":{"scopes":"mcp:operate"}}'
claude mcp login datum-sync --no-browser     # open the URL as the sponsor; pick the agent on the consent page
```

**Claude Code — rotating baseline token via helper** (for agents whose
token an operator rotates without editing config):

```json
{"mcpServers": {"datum-sync": {"type": "http", "url": "https://sync.example.com/mcp",
  "headersHelper": "cat /etc/datum-sync/headers.json"}}}
```

**Codex — baseline token**

```toml
[mcp_servers.datum-sync]
url = "https://sync.example.com/mcp"
bearer_token_env_var = "DATUM_SYNC_TOKEN"
```

**Codex — elevated (OAuth)**

```bash
codex mcp add datum-sync --url https://sync.example.com/mcp
codex mcp login datum-sync       # choose the scope and the agent on the consent page
```

**Datum-3.0 — baseline token** (`mcp_desktop.json` or `~/.claude.json`,
which the bridge already reads):

```json
{"mcpServers": {"datum-sync": {"type": "http", "url": "https://sync.example.com/mcp"}}}
```

with `DATUM_MCP_AUTH_TOKEN=<token>` and `DATUM_MCP_SERVERS=datum-sync` in
the service environment. The bridge's single `DATUM_MCP_AUTH_TOKEN` applies
to every HTTP server it knows; a per-server token (`headers` in the server
config) is a small bridge change and is listed as a Datum-3.0 task.

## 5. Conformance script

`tests/harness_conformance.py` drives the gateway the way each client
does, using the Python `mcp` SDK for the wire and recorded request shapes
for the two CLIs' OAuth steps:

1. **Baseline, Claude-Code-shaped**: `initialize` with `clientInfo.name =
   "claude-code"`, `tools/list`, one `tools/call`, then a second
   `initialize` on the same token without `DELETE` → admitted, first
   session `superseded`.
2. **Baseline, Datum-3.0-shaped**: five sequential `initialize` → `tools/call`
   → `DELETE` cycles on one token → five ended sessions, never a
   `SESSION_LIMIT`; a sixth `initialize` from a *second* token → `SESSION_LIMIT`.
3. **OAuth, Codex-shaped**: `POST /oauth/register` with no `scope`, `client_name = "Codex CLI 0.x"`, one loopback redirect; authorize with `scope=mcp:operate`; consent as sponsor with `on_behalf_of`; token; `whoami.effective_tier == 3` and `whoami.name` is the agent.
4. **OAuth, Claude-Code-shaped**: register; authorize with **no** scope → consent page shows the picker; choose `mcp` → `effective_tier ≤ 2`.
5. **Tool names**: every name in `tools/list` for a principal with federation to the mock upstream is ≤ 48 characters and matches `^[A-Za-z0-9_.-]+$`.
6. **Refresh**: the Claude-Code-shaped client presents a rotated refresh token twice → family revoked, next `tools/call` 401.

The script is part of WP3 (cases 1, 2, 5) and WP4 (3, 4, 6).

## 6. Guards

| id | Guard | Test must fail when |
|---|---|---|
| SESS-005 | supersession only for the same credential | credential comparison removed |
| SESS-006 | per-principal idle window applied | `limits.session_idle_seconds` ignored |
| ELEV-011 | consent scope picker cannot offer a scope above the principal's ceiling | filter removed |
| ELEV-012 | registration without `scope` accepted; authorize scope still validated | vocabulary check moved to register |
| MCP-020 | tool names ≤ 48 chars, safe charset, stable under collision | truncation removed |

## Sources

- Claude Code MCP documentation: https://code.claude.com/docs/en/mcp (transport, `--header`, `headersHelper`, `oauth.scopes`, `claude mcp login --no-browser`, tool-name limits, reconnection, "device flow not supported")
- Codex: openai/codex issues #19154, #20503, #23242, #23627, #20009 (DCR default, `--oauth-client-registration dcr|cimd`, scope omission at registration, static client id in older builds)
- Datum-3.0: `datum_mcp/mcp_bridge.py` at `56b0fbe` (per-call HTTP sessions, `DATUM_MCP_AUTH_TOKEN`, config sources), `server.py` (`claude` subprocess), `datum_mcp/oauth.py` (server-side OAuth for claude.ai, not a client)
