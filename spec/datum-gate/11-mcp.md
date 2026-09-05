# 11 — MCP Surface

## 1. Transport

`POST /mcp`, Streamable HTTP, JSON-RPC 2.0, protocol version `2025-06-18`.

- **Stateless.** No session id is issued or required; the bearer token on
  every request identifies the principal. `Mcp-Session-Id` is ignored if
  sent.
- `GET /mcp` → 405 with `Allow: POST`. The server has nothing to push:
  long-running tool calls are handled by the job-handle pattern (§5), not by
  a server stream.
- Batching (JSON arrays) → `-32600`.
- Notifications (no `id`) → 202 with an empty body.
- Every response carries `MCP-Protocol-Version: 2025-06-18`.
- A 401 carries `WWW-Authenticate: Bearer resource_metadata="{PUBLIC_URL}/.well-known/oauth-protected-resource"`.
- Every request writes one `audit_log` row with `via='mcp'` (`14 §3`).

## 2. Methods

| Method | Handler |
|---|---|
| `initialize` | echoes protocol version if supported, else ours; `capabilities: {tools: {listChanged: false}, resources: {subscribe: false, listChanged: false}}`; `serverInfo: {name: "datum-gate", version}` |
| `ping` | `{}` |
| `tools/list` | §3 |
| `tools/call` | §4–§6 |
| `resources/list` | §7 |
| `resources/read` | §7 |
| `resources/templates/list` | §7 |
| anything else | `-32601` |

## 3. Catalogue (`tools/list`)

Built per request from the caller's effective grant; never cached.

**Workspace tools.** One per workspace whose current version declares
`mcp` in `services` and whose repository is in the caller's scope. Name
`{repo}__{ws}` (unsafe chars → `_`, ≤128). Two workspaces mapping to one
name is an operator error and raises `-32603` naming both.

```json
{
  "name": "SCIMAC__site_plan",
  "title": "Site plan",
  "description": "<manifest.mcp.description_for_agents or manifest.description>\n\n<first paragraph of MANIFEST.md Purpose>",
  "inputSchema": {
    "type": "object",
    "properties": {
      "JOB_ID": {"type": "string", "description": "Stratum job number", "pattern": "^[0-9]{4,6}$"},
      "OUTPUT_FORMAT": {"type": "string", "enum": ["HTML","PDF","JSON"], "default": "HTML"},
      "_wait_seconds": {"type": "integer", "minimum": 0, "maximum": 300, "default": 45,
                        "description": "How long to wait for the result before returning a job handle"}
    },
    "required": ["JOB_ID"]
  },
  "annotations": {"readOnlyHint": false, "idempotentHint": false}
}
```

Parameter → schema mapping: `STRING`→string(+pattern,maxLength),
`INTEGER`→integer(+min/max), `FLOAT`→number, `BOOLEAN`→boolean,
`CHOICE`→string enum, `GEOMETRY`→string with `description` "WKT or GeoJSON",
`JSON`→the declared schema or `object`, `FILE`→**omitted** (optional only;
required FILE workspaces cannot declare `mcp`).

**Built-in tools**, shown only when the grant makes them usable:

| Tool | Shown when | Tier |
|---|---|---|
| `whoami` | always | 1 |
| `job_status` | always | 2 |
| `job_result` | always | 2 |
| `job_cancel` | always | 3 (or submitter) |
| `job_list` | always | 2 |
| `vault_read`, `vault_list`, `vault_stat`, `vault_search` | `vault.read` non-empty | 1 |
| `vault_write`, `vault_delete` | `vault.write` non-empty | 3 |
| `quarantine_write` | `vault.quarantine` non-empty | 2 |
| `quarantine_promote` | `vault.promote` non-empty | 3 |
| `promotion_approve`, `promotion_reject`, `promotion_list` | tier ≥4 | 4 |
| `proxy_request` | `connections.proxy` non-empty | 3 |
| `connection_list` | always | 2 (names, types, tiers — no config) |
| `schedule_create`, `schedule_list` | always | 3 / 2 |
| `delegate_create` | tier ≥3 | 3 — creates a child agent principal and returns its token once (`§8`) |
| `delegate_revoke` | tier ≥3 | 3 |

Fewer tools rather than broken ones: a tool that could only ever return
403 for this caller is not listed.

## 4. `tools/call` dispatch

```
name = params.name; args = params.arguments or {}
if name in BUILTINS: dispatch (tier + scope checks inside; ApiError → isError result)
else:
  found = catalogue(principal); if name not in found: -32602 "unknown tool"   # same answer for hidden and nonexistent
  repo, ws, version = found[name]
  wait = args.pop('_wait_seconds', manifest.mcp.wait_default_seconds or 45)
  submit (06 §2) with triggered_by='mcp:{client_id or principal}', all args coerced by the manifest
  await_job(job_id, min(wait, timeout+15))
  on completion → result (§6)
  on timeout   → job handle (§5)
  on failure   → isError result with the job's error and last 50 log lines
```

A failed workspace is a **tool error** (`isError: true`), not a JSON-RPC
error: the model must see it to react.

## 5. Long-running tools: the job handle

If the job does not finish within `_wait_seconds`, `tools/call` returns:

```json
{
  "content": [{"type": "text",
    "text": "Job 6f1c… is still running (started 47s ago, 12% \"Rendering sheets\").\nCall job_status with job_id=6f1c… to check, or job_result to fetch the output when it completes."}],
  "structuredContent": {"job_id": "6f1c…", "status": "running", "pct": 0.12, "resource": "datum://jobs/6f1c…"},
  "isError": false
}
```

`job_status(job_id)` → status, progress (if streaming), elapsed, last 5 log
lines. `job_result(job_id, max_inline_bytes?)` → the same content blocks a
completed call would have returned, or the handle again if still running.
Both refuse (isError) jobs outside the caller's scope.

This pattern is what lets a workspace with a 20-minute timeout be an MCP
tool without holding an HTTP connection open for 20 minutes.

## 6. Results

For each artifact in manifest order, primary first:

- `text/*`, `application/json`, `application/xml`, `text/markdown` →
  inline `text` block, truncated at `MCP_MAX_INLINE_BYTES` (64 KiB) with a
  trailer pointing at the resource URI
- `image/png|jpeg|gif|webp` ≤ 1 MiB → inline `image` block (base64) — the
  one binary case inlined, because vision-capable clients can use it
- everything else → a `resource_link` block:
  `{"type":"resource_link","uri":"datum://jobs/{id}/artifacts/{name}","name":…,"mimeType":…,"size":…}`
- `service/*` → text block with the absolute `/serve/{name}/` URL

`structuredContent` always carries `{job_id, status, artifacts:[{name,type,size,uri}], duration_seconds}`.

No artifacts → text "The workspace produced no output." plus the job id.

## 7. Resources

Resources expose things the caller can already read via REST, in MCP form:

| URI template | Read returns |
|---|---|
| `datum://jobs/{job_id}` | job JSON |
| `datum://jobs/{job_id}/log` | text log |
| `datum://jobs/{job_id}/artifacts/{name}` | the artifact (text inline; binary as `blob` base64 up to 4 MiB, else a text block with the REST URL) |
| `datum://vault/{path}` | vault read under the caller's scope |
| `datum://workspaces/{repo}/{ws}/doc` | MANIFEST.md |

`resources/list` returns the caller's 50 most recent jobs' primary
artifacts and nothing from the vault (listing the vault is `vault_list`,
which is scope-aware per directory). `resources/templates/list` returns
the five templates above.

## 8. Delegation from MCP

`delegate_create({name, grant, expires_in_seconds?})`:

- creates a child `agent` principal under the caller (`03 §5`), with the
  given grant, which must narrow the caller's; tier-3 callers may create
  tier ≤2 children
- mints one token with the given expiry (default 24h, max
  `policy.delegation.max_token_hours`)
- returns `{principal, token}` — the raw token once, in a text block and in
  `structuredContent`

This is how a dispatching agent creates a research drone with a sliced vault
scope without an operator in the loop, and `delegate_revoke({name})`
disables the child and revokes its credentials.

## 9. Trace propagation

The MCP request's `trace_id` (server-minted) is written to the job, to
deliveries it causes, and to every audit row. `X-Trace-Id` from the client
is stored as `client_trace_id`. `_meta.trace_id` in every result carries the
server value back so the agent can correlate.
