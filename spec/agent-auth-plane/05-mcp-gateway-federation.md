# 05 — MCP gateway: federating upstream MCP servers

This is the "MCP proxy" half of the brief. The gateway becomes an MCP
*client* to servers the company already runs — a GitHub MCP server, an SSH
or VM server, a Google Drive server, internal tool servers — and projects
their tools into the one `tools/list` an agent sees, filtered by the
agent's grant and checked argument by argument before anything is
forwarded. Model and mechanism come from datum-gate `21`; the guard grammar
and the mapping persistence are redone here because the review showed the
originals could not express their own examples.

## 1. An upstream is a connection

New `connections.type = 'mcp'` beside the existing `database`, `http`,
`email_smtp`, `email_imap`, `file` and `oauth_client`. `connections.validate`
gains the type; the Connections screen gains a form.

```json
{
  "name": "github",
  "type": "mcp",
  "tier": 3,
  "scope": "global",
  "config": {
    "url": "https://mcp.github.internal/mcp",
    "transport": "streamable_http",
    "resource_kind": "code",
    "tool_prefix": "gh",
    "headers": {"X-Org": "datum"},
    "auth_inject": {"type": "bearer", "secret_field": "token"},
    "refresh_seconds": 300,
    "timeout_seconds": 60,
    "allow_private_origin": true,
    "guards": { "…": "§4" },
    "default": "deny"
  },
  "secret": {"token": "ghp_…"}
}
```

- `resource_kind ∈ code | compute | documents | other` selects which block
  of `federation_scope` governs the connection (§2).
- `auth_inject` reuses `proxy.inject_auth` unchanged. The agent never sees
  the upstream credential; `Authorization` from the inbound request is
  never forwarded (guard `FED-011`).
- `allow_private_origin` may be set only by tier 5 (company MCP servers live
  on the LAN). `proxy.validate_upstream_url` gains a parameter for it; the
  default stays deny.
- `transport` is `streamable_http` only in phase 1. stdio upstreams are a
  process, and this gateway does not spawn processes for anything but jobs.
- OAuth-backed upstreams (the gateway holding a refresh token for, say, a
  hosted Drive server) are phase 2 (`11 D-13`); phase 1 uses static
  secrets.

## 2. The `federation_scope` blocks

```json
{
  "code":      {"connections": ["github"], "repos": ["datum/*", "scimac/geo-*"], "write": false,
                "tools": {"allow": ["*"], "deny": ["*delete*", "*force*"]}},
  "compute":   {"connections": ["vm-lan"], "hosts": ["vm102", "vm111"],
                "commands": {"allow": ["^(ls|cat|tail|systemctl status|journalctl|df|ps)\\b"], "deny": ["\\brm\\b", "\\bsudo\\b", "\\bdd\\b", ">"]},
                "paths": {"read": ["/home/ubuntu/**", "/var/log/**"], "write": []},
                "approval_required": ["restart", "deploy"],
                "tools": {"allow": ["*"], "deny": []}},
  "documents": {"connections": ["gdrive"], "folders": ["1AbC…"], "write": false, "share": false,
                "tools": {"allow": ["*"], "deny": []}},
  "mcp":       {"connections": {"other-server": {"tools": {"allow": ["search_*"], "deny": []}}}}
}
```

`tools.{allow,deny}` exists on **every** block (the corpus had it on `code`
only and tested it everywhere). Deny wins; a tool matching no allow is not
listed.

### 2.2 Narrowing (for `grants.narrows`)

connection lists ⊆ parent's; `repos`/`hosts`/`folders` each subsumed by a
parent pattern (`grants.subsumes` on `/`-separated globs); `write`/`share`
false unless parent true; `commands.allow` ⊆ parent's **by string equality**
(regexes are not subsumed); `commands.deny` ⊇ parent's; `paths.read/write`
subsumed; `approval_required` ⊇ parent's; `tools.allow` ⊆ parent's by string
equality, `tools.deny` ⊇ parent's.

## 3. Catalogue federation

### 3.1 Refresh

A worker tick (every `refresh_seconds`, per connection, and on connection
save) does `initialize` + `tools/list` (+ `resources/templates/list`)
against the upstream with injected auth, 5 s timeout, and upserts
`federated_tools`:

```sql
federated_tools (
  connection     TEXT REFERENCES connections(name) ON DELETE CASCADE,
  upstream_name  TEXT,
  tool_name      TEXT UNIQUE,          -- "{prefix}__{upstream_name}", unsafe → "_", ≤128
  description    TEXT, input_schema JSONB, annotations JSONB,
  guarded        BOOLEAN,              -- some guard matches this tool
  fetched_at     TIMESTAMPTZ, PRIMARY KEY (connection, upstream_name)
)
```

plus `connections.federation_status JSONB` `{last_ok_at, last_error,
tool_count}`. The **mapping is a table**, so a `tools/call` on any process
resolves the same name (fixes the corpus's in-memory `mapping[name]`), and
an unavailable upstream serves its last catalogue rather than nothing.

Name collision between two upstreams or with a workspace tool → the later
one is not inserted and `federate.name_clash` is audited; the Federation
screen shows it. Workspace tools win because they are local and named by an
administrator.

### 3.2 `tools/list` for a principal

For each `mcp` connection named in any `federation_scope` block the
principal holds, with `principal.effective_tier ≥ connection.tier`:
`federated_tools` rows for that connection → drop those failing the block's
`tools` allow/deny → drop those with `guarded = false` when `default =
deny` → drop those whose guard `requires` the principal cannot meet
(`write:true` on a read-only block, `tier` above effective) → annotate
`description` with `[via {connection}]` → merge. `tools/list` never touches
an upstream. An upstream that is stale beyond `3 × refresh_seconds` is
still listed; its calls fail fast with `isError` naming the outage.

`resources/list` and `resources/templates/list` federate the same way with
URIs rewritten to `datum://{connection}/{upstream_uri}`; `resources/read`
is guarded (§4.5).

## 4. Argument guards

Guards live on the connection (`config.guards`), so an administrator can
adapt to any upstream's schema without a code change. They are validated
against the cached `input_schema` at save and refresh; a guard naming an
argument the tool does not declare is a UI warning and a
`federate.guard_mismatch` audit row, and **the guard still denies** (a
guard that cannot find its argument is a missing argument, §4.3).

### 4.1 Grammar

```json
"guards": [
  {"tools": ["get_file_contents", "list_commits", "search_code", "get_pull_request*"],
   "checks": [{"grant": "repos", "value": {"join": ["$.owner", "$.repo"], "sep": "/"}}]},

  {"tools": ["push_files", "create_branch", "create_pull_request", "merge_pull_request"],
   "checks": [{"grant": "repos", "value": {"join": ["$.owner", "$.repo"], "sep": "/"}},
              {"grant": "paths.write", "value": {"path": "$.files[*].path"}, "optional": true}],
   "requires": {"write": true}},

  {"tools": ["delete_*"], "requires": {"tier": 5}},

  {"tools": ["run_command"],
   "checks": [{"grant": "hosts", "value": {"path": "$.host"}},
              {"grant": "commands", "value": {"path": "$.command"}}]},

  {"tools": ["read_file"],
   "checks": [{"grant": "hosts", "value": {"path": "$.host"}},
              {"grant": "paths.read", "value": {"path": "$.path"}}]},

  {"tools": ["write_file"],
   "checks": [{"grant": "hosts", "value": {"path": "$.host"}},
              {"grant": "paths.write", "value": {"path": "$.path"}}],
   "requires": {"write": true}},

  {"tools": ["restart_service", "deploy"], "requires": {"approval": "restart"}},

  {"tools": ["search", "get_file", "list_folder"],
   "checks": [{"grant": "folders", "value": {"path": "$.folderId", "resolve": "drive_folder"}}]},
  {"tools": ["share_*"], "requires": {"share": true}}
]
```

- `tools`: globs over upstream tool names (`fnmatch`-style on the *tool
  name only*; `*` may cross `_`).
- `checks[]`: each yields one or more **values** from the arguments and
  requires every value to match some pattern in the named **grant field**
  of the governing block. Grant fields: `repos`, `hosts`, `folders`,
  `paths.read`, `paths.write`, `commands` (special, below).
- `value` forms:
  - `{"path": "<jsonpath>"}` — a restricted JSONPath: `$`, `.name`, `[*]`,
    `[n]`. `[*]` fans out; a list value fans out; every element must pass.
  - `{"join": ["<path>", …], "sep": "/"}` — each path must resolve to a
    single scalar; the scalars are joined. This is the `owner/repo` case.
  - `{"const": "…"}` — for tools whose target is fixed (a server that only
    talks to one repo).
  - `"resolve": "drive_folder"` — the one lookup guard: a file id is
    resolved to its ancestor folder chain via the upstream's own `get_file`,
    cached 10 min; failure to resolve is a deny.
- `optional: true` — the check is skipped when the path resolves to nothing
  (used for arguments the tool may omit). Default false: **a missing
  guarded argument is a deny** (a missing repo is not "any repo").
- `commands` semantics: the value is tested against the block's
  `commands.deny` regexes first (any match → deny), then `commands.allow`
  (any match → allow), else deny. Regexes are compiled once at grant write
  with `re.compile` and a 1 KiB length cap; `re2` is not a dependency.
- `requires`: `{write: true}`, `{share: true}`, `{tier: n}` (against
  `effective_tier`), `{approval: "<label>"}` (§5). A tool may match several
  guards; **all** matching guards apply.
- `default`: `deny` (unguarded tools are not listed) or `allow` (listed,
  forwarded with no argument check, audited `guard: none`). Default `deny`.

Values are matched with `grants.matches` — the vault glob rules (`*` is one
segment, `**` any depth), applied to `/`-separated targets. For `hosts` and
`folders` there is no separator, so `*` alone means any.

### 4.2 Evaluation order on `tools/call`

```
row = federated_tools[name]                          else -32602 unknown tool
block = federation_scope[row.connection.resource_kind]
row.connection ∈ block.connections                   else -32602 (same answer; never reveal)
effective_tier ≥ connection.tier                     else isError TIER_REQUIRED
tool allow/deny                                      else -32602
guards = matching guards; none and default=deny      → -32602
for g in guards: requires → checks (values extracted, matched)   any failure → isError FEDERATION_DENIED {guard label, grant field}
if any g.requires.approval and label ∈ block.approval_required → pending (§5)
audit federate.call (target "{connection}:{upstream}", detail = guarded values only) BEFORE forwarding
forward tools/call with injected auth; timeout; 1 MiB result cap; strip upstream _meta headers
audit outcome ok|error|denied, duration
return content blocks verbatim; structuredContent preserved
```

The `denied` outcome is written for a guard refusal. This is the first
writer of `denied`, so migration 021 widens `audit_log.outcome`'s CHECK; the
`mcp_call_log` mirror keeps writing `ok` for `isError` results as today, and
that disagreement is the documented reason the mirror retires in `09 WP7`.

### 4.3 Missing and malformed arguments

A check whose path resolves to nothing, to a non-scalar where a scalar is
required, or to a non-string scalar → deny (`FED-005`). Arguments larger
than 256 KiB are refused before evaluation.

### 4.4 Reference profiles

Shipped as data under `datum_sync/federation/profiles/*.json`, selectable
in the connection form, editable afterwards: `github-mcp`, `gitea-mcp`,
`local-git-mcp`, `ssh-mcp`, `gdrive-mcp`. Each is a `config` template with
guards written in the grammar above against that server's documented tool
names. A profile is a starting point; the guard validation at save reports
which of its tool globs matched nothing on the real upstream.

### 4.5 Resource guards

`resources/read` on `datum://{connection}/{uri}` is governed by the block's
`resource_guards`: a list of `{"uri": "<glob>", "grant": "<field>",
"value": {"regex_group": "…"}}` — the upstream URI is matched to a glob and
a named regex group extracts the value to check (a repo, a folder id, a
path). No resource guard matching → deny when `default = deny`. Guard
`FED-016`.

## 5. Approval-gated calls

`pending_calls(id, connection, tool, args JSONB, requested_by, requested_name,
requested_at, session_id, trace_id, label, status CHECK IN ('pending','approved','rejected','expired','executed','failed'),
decided_by, decided_name, decided_at, result JSONB, grant_snapshot JSONB)`.

The agent receives `{status: "pending_approval", pending_id, label,
expires_at}` in `structuredContent` and `isError: false`. MCP built-ins
`pending_status(id)`, `pending_result(id)`; REST for the Approvals screen.
Approval (tier ≥4, or the requester's sponsor when the tool's block is ⊆
the sponsor's own) forwards the call **under the requester's
`grant_snapshot` taken at request time**, stores the result, and the agent
fetches it. This is the one place argument *values* are stored — for the
approver — and the row is governance-flagged. Pending calls expire after
`POLICY_PENDING_CALL_TTL_HOURS` (72).

## 6. Health and observability

- `/health.federation`: per connection `{status: ok|stale|down, tool_count, last_ok_at}`.
- The Federation screen (`08 §5`).
- `audit_log` verbs: `federate.refresh`, `federate.unavailable`,
  `federate.name_clash`, `federate.guard_mismatch`, `federate.call`,
  `federate.denied`, `federate.pending`, `federate.approve|reject|execute`.

## 7. Seam for a memory provider

If shared memory is wanted, it is a built-in provider beside the vault:
`memory_*` tools, a `memory_scope` column shaped like `vault_scope`, and
`vault.py`'s glob rules on namespaces. datum-gate `21 §3` is a reasonable
DDL; the review's soft-delete and ranking contradictions must be resolved
first. Not in this build plan.

## 8. Guards

| id | Guard | Test must fail when |
|---|---|---|
| FED-001 | federated tools listed only for connections in the block | connection filter removed |
| FED-002 | block `tools` allow/deny applied on every block | filter removed from catalogue build |
| FED-003 | `default: deny` hides unguarded tools | `guarded` filter removed |
| FED-004 | value outside the grant glob → denied before forwarding (mock asserts zero upstream requests) | `matches` result ignored |
| FED-005 | missing guarded argument → denied | `optional` default flipped |
| FED-006 | `requires.write` enforced | check removed |
| FED-007 | `requires.tier` against effective tier | check removed or reads `max_tier` |
| FED-008 | commands: deny regexes before allow | order swapped |
| FED-009 | approval-gated call not forwarded until approved | status check removed from execute |
| FED-010 | approved call runs under the requester's snapshot | live grant used |
| FED-011 | inbound `Authorization` never forwarded; upstream secret never in a result | header strip removed |
| FED-012 | upstream down → tool omitted or fails fast; `tools/list` answers | exception propagated |
| FED-013 | only guarded values in audit detail | full args logged |
| FED-014 | Drive folder resolution failure → deny | fallback to allow |
| FED-015 | child federation blocks must narrow the parent's | `narrows` block check removed |
| FED-016 | `resources/read` guarded | guard evaluation removed |
| FED-017 | `join` value form denies when any path is non-scalar | scalar check removed |
| FED-018 | private-origin upstream requires tier 5 at save | tier check removed |
| FED-019 | name clash with a workspace tool: workspace wins, upstream tool dropped and audited | insert allowed |
| FED-020 | argument size cap | cap removed |
