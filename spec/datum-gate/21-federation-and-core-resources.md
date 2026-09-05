# 21 — Federation and the Core Company Resources

## 1. The reframing

An agent is barebones on its own: a model, a loop, a local toolset. The
gateway is what makes it a member of the organisation. Once it holds a
principal and a grant, the **one MCP endpoint** it is given exposes — in
addition to workspaces and the vault — the company's core resources:

| Resource | What it is | Provided by |
|---|---|---|
| **Memory** | shared company memory in Postgres: facts, notes, decisions, entity records, searchable | built into the gateway (it already owns a Postgres) |
| **Code** | the codebases and repositories behind the company's services | federated to a code MCP server (GitHub / Gitea / local git server) |
| **Compute** | access to VMs: run commands, read files, inspect services | federated to a VM/SSH MCP server per host group |
| **Documents** | Google Drive resources | federated to a Drive MCP server |

The agent never configures four servers, never holds four credentials,
and never sees a tool its grant does not reach. Every call crosses the
gateway, is checked against the grant (down to the *arguments*), and lands
in the audit spine.

Two mechanisms carry this: **built-in providers** (memory) and
**federated upstreams** (everything else). Both project into the same
`tools/list`.

## 2. Federated upstreams

### 2.1 An upstream is a connection

New connection type `mcp`:

```json
{
  "name": "github",
  "type": "mcp",
  "tier": 3,
  "scope": "global",
  "config": {
    "url": "https://mcp.github.internal/mcp",
    "transport": "streamable_http",
    "tool_prefix": "gh",
    "resource_kind": "code",
    "headers": {"X-Org": "datum"},
    "auth_inject": {"type": "bearer", "secret_field": "token"},
    "refresh_seconds": 300,
    "timeout_seconds": 60
  },
  "secret": {"token": "ghp_…"}
}
```

- `resource_kind` is one of `code | compute | documents | other`; it selects
  which grant block governs the connection (§4) and which built-in argument
  guards apply (§5).
- Auth is injected exactly as the credential proxy does (`07 §6`); the
  agent never sees the upstream token. OAuth-backed upstreams use an
  `oauth_client` connection named in `config.oauth_connection`; the gateway
  holds the refresh token and mints access tokens.
- Egress rules apply (`09 §7`): private origins are allowed for `mcp`
  connections because company MCP servers live on the LAN — tier-5 creates
  them, and every call is audited.

### 2.2 Catalogue federation

On `tools/list`, for each `mcp` connection the caller's grant reaches:

1. fetch (or serve from cache, TTL `refresh_seconds`) the upstream's
   `tools/list` via `initialize` + `tools/list` with injected auth
2. filter by the grant's tool allow/deny for that connection (§4)
3. rewrite each tool name to `{tool_prefix}__{upstream_name}` (≤128 chars,
   unsafe chars → `_`) and record the mapping
4. annotate the description with `[via {connection}]`
5. merge with workspace tools, vault tools and memory tools

An upstream that is down is **omitted with an audit row**
(`federate.unavailable`), not an error to the agent: fewer tools rather
than a broken list. `tools/list` never blocks longer than 5 s per upstream
in parallel; a slow upstream serves its last cached catalogue.

`resources/list` and `resources/templates/list` federate the same way, URIs
rewritten to `datum://{connection}/{upstream_uri}`.

### 2.3 Call federation

`tools/call` on a federated name:

```
conn, upstream_tool = mapping[name]
principal.may_use_mcp(conn)                         # grant block + tier ≥ conn.tier
guards.check(conn.resource_kind, upstream_tool, args, principal.effective)   # §5
audit.write_sync(verb='federate.call', target=f'{conn}:{upstream_tool}', args_summary)  # before forwarding
forward tools/call to upstream with injected auth, timeout, size cap on result
audit outcome (ok | error | denied), duration
return the upstream result verbatim (content blocks pass through; structuredContent preserved)
```

`args_summary` is the **shape** of the arguments — key names and, for
guarded keys, the value that was checked (a repo name, a host, a folder id)
— never free-text values. Denied calls are written synchronously and
flagged `governance` when the guard says so.

Notifications, prompts and sampling are not federated: the gateway is a
tool and resource broker, not a full MCP relay.

## 3. Built-in provider: Memory

Shared company memory lives in the gateway's Postgres so it is scoped,
audited and searchable without a second service.

### 3.1 Schema (migration `010_memory.sql`)

```sql
CREATE TABLE memory_namespaces (
    name        TEXT PRIMARY KEY CHECK (name ~ '^[a-z0-9][a-z0-9/_-]{0,127}$'),  -- e.g. "org/decisions", "team/geo/clients"
    description TEXT,
    team_id     INTEGER REFERENCES teams(id) ON DELETE SET NULL,   -- 23-tenancy.md
    created_by  TEXT, created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE memory_entries (
    id           BIGSERIAL PRIMARY KEY,
    namespace    TEXT NOT NULL REFERENCES memory_namespaces(name) ON DELETE CASCADE,
    key          TEXT NOT NULL,                       -- stable identifier within the namespace
    kind         TEXT NOT NULL CHECK (kind IN ('fact','note','decision','entity','event','summary')),
    title        TEXT,
    body         TEXT NOT NULL,                       -- markdown/plain, ≤ 64 KiB
    data         JSONB NOT NULL DEFAULT '{}'::jsonb,  -- structured fields for entity/event kinds
    tags         TEXT[] NOT NULL DEFAULT '{}',
    links        TEXT[] NOT NULL DEFAULT '{}',        -- "namespace/key" references
    confidence   REAL CHECK (confidence IS NULL OR confidence BETWEEN 0 AND 1),
    valid_from   TIMESTAMPTZ, valid_to TIMESTAMPTZ,   -- temporal validity for facts
    embedding    VECTOR(1536),                        -- pgvector, optional; NULL when no embedder configured
    tsv          TSVECTOR GENERATED ALWAYS AS (to_tsvector('english', coalesce(title,'') || ' ' || body)) STORED,
    version      INTEGER NOT NULL DEFAULT 1,
    created_by   TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_by   TEXT NOT NULL, updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    trace_id     UUID NOT NULL,
    UNIQUE (namespace, key)
);
CREATE INDEX memory_entries_tsv_idx  ON memory_entries USING GIN (tsv);
CREATE INDEX memory_entries_tags_idx ON memory_entries USING GIN (tags);
CREATE INDEX memory_entries_ns_idx   ON memory_entries (namespace, updated_at DESC);

-- Every write keeps the prior version. Memory is an append-only history
-- with a current view; agents can be wrong, and "what did we believe last
-- Tuesday" must be answerable.
CREATE TABLE memory_history (
    id          BIGSERIAL PRIMARY KEY,
    entry_id    BIGINT NOT NULL,
    version     INTEGER NOT NULL,
    body        TEXT NOT NULL, data JSONB NOT NULL, tags TEXT[] NOT NULL, links TEXT[] NOT NULL,
    changed_by  TEXT NOT NULL, changed_at TIMESTAMPTZ NOT NULL, trace_id UUID NOT NULL,
    change_kind TEXT NOT NULL CHECK (change_kind IN ('create','update','delete','restore'))
);
```

`pgvector` is optional: if the extension is absent the column is omitted by
a conditional migration and search is full-text only. An embedder, when
configured, is an `http` connection named in `policy.memory.embedder_connection`;
the gateway calls it on write (fire-and-forget, backfilled by the worker).

### 3.2 Grant block

```json
"memory": {
  "read":  ["org/**", "team/geo/**"],
  "write": ["team/geo/**", "agents/datum-main/**"],
  "deny":  ["org/hr/**"]
}
```

Namespace globs use the vault glob rules. Deny wins. Write implies read
coherence applies. Every agent gets a private namespace
`agents/{name}/**` by default at registration (`22-agent-lifecycle.md`).

### 3.3 Tools

| Tool | Tier | Behaviour |
|---|---|---|
| `memory_search(query, namespaces?, kinds?, tags?, limit, since?)` | 1 | full-text (and vector when available) search across readable namespaces; returns `{namespace, key, kind, title, snippet, score, updated_at}`; ≤ 50 |
| `memory_get(namespace, key, version?)` | 1 | one entry, current or historical |
| `memory_list(namespace, prefix?, kind?, limit, cursor?)` | 1 | keys and titles |
| `memory_put(namespace, key, kind, title?, body, data?, tags?, links?, confidence?, expect_version?)` | 2 | create or update; `expect_version` gives optimistic concurrency (409 on mismatch); writes history |
| `memory_delete(namespace, key)` | 3 | soft-delete: history row `delete`, entry removed from current view |
| `memory_history(namespace, key)` | 1 | versions with who/when/trace |
| `memory_link(from, to)` / `memory_unlink` | 2 | maintain `links` both ways |
| `memory_namespaces()` | 1 | readable namespaces with descriptions |

Writes into namespaces under `org/**` are governance-flagged and require
`policy.memory.org_write_min_tier` (default 4). Every tool audits
`memory.{verb}` with `target = "{namespace}/{key}"`; bodies never in audit.

MCP resources: `datum://memory/{namespace}/{key}` (read) and
`datum://memory/{namespace}` (listing).

REST: `/rest/v1/memory/…` mirrors the tools for the UI and scripts.

## 4. Grant blocks for the federated kinds

The `Grant` document gains one block per resource kind. Each names which
connections of that kind the principal may use and constrains arguments.

```json
"code": {
  "connections": ["github"],
  "repos": ["datum/*", "scimac/geo-*"],
  "write": false,
  "tools": {"allow": ["*"], "deny": ["*delete*", "*force*"]}
},
"compute": {
  "connections": ["vm-lan"],
  "hosts": ["vm102", "vm111"],
  "commands": {"allow": ["^(ls|cat|tail|systemctl status|journalctl|df|ps)\\b"], "deny": ["\\brm\\b", "\\bsudo\\b", "\\bdd\\b", ">"]},
  "paths": {"read": ["/home/ubuntu/**", "/var/log/**"], "write": []},
  "approval_required": ["restart", "deploy"]
},
"documents": {
  "connections": ["gdrive"],
  "folders": ["1AbC…", "1XyZ…"],
  "write": false,
  "share": false
},
"mcp": {
  "connections": {"other-server": {"tools": {"allow": ["search_*"], "deny": []}}}
}
```

Narrowing rules (`03 §5`) extend naturally: connection lists ⊆ parent's,
repo/host/folder globs subsumed, `write`/`share` false unless parent true,
`commands.allow` ⊆ parent's by string equality (regexes are not subsumed —
a child may only reuse patterns the parent holds), `commands.deny` ⊇
parent's, `approval_required` ⊇ parent's.

## 5. Argument guards

Federated tools are opaque; the gateway does not know what `push` or
`run_command` mean. **Argument guards** map upstream tool arguments onto
grant fields so the check is still deterministic. Guards are declared on
the connection so an admin can adapt to any upstream server's schema:

```json
"config": {
  "resource_kind": "code",
  "guards": [
    {"tools": ["get_file_contents", "list_commits", "search_code"],
     "args": {"repo": "repos"}},
    {"tools": ["push_files", "create_branch", "create_pull_request", "merge_pull_request"],
     "args": {"repo": "repos"}, "requires": {"write": true}},
    {"tools": ["delete_*"], "requires": {"tier": 5}}
  ],
  "default": "deny"
}
```

Semantics:

- `tools`: glob over upstream tool names.
- `args`: `{argument_path: grant_field}` — the argument's value (a string,
  or every string in a list) must match some glob in the named grant field
  (`repos`, `hosts`, `folders`, `paths.read`, `paths.write`); `argument_path`
  is a dotted path into the arguments object.
- `requires`: `{write: true}`, `{share: true}`, `{tier: n}`, `{approval: "label"}`.
- `commands` guard (compute only): `{"args": {"command": "commands"}}`
  checks the value against `commands.deny` first (any match → deny), then
  `commands.allow` (any match → allow), else deny.
- `default`: what happens to a tool no guard matches — `deny` (the tool is
  not even listed) or `allow` (listed, forwarded with no argument check;
  audited `guard: none`). Default `deny`.
- A guard whose `args` name an argument the call did not supply → deny
  (a missing repo is not "any repo").

Guards are validated against the upstream's cached `inputSchema` when the
connection is saved or refreshed: a guard naming an argument the tool does
not declare is a warning in the UI and a `federate.guard_mismatch` audit
row.

### 5.1 Approval-gated calls

A guard with `requires.approval` or a compute tool matching the grant's
`approval_required` list creates a **pending call** instead of forwarding:

```sql
CREATE TABLE pending_calls (
    id            BIGSERIAL PRIMARY KEY,
    connection    TEXT NOT NULL, tool TEXT NOT NULL,
    args          JSONB NOT NULL,                     -- stored: an approver must see what they approve
    requested_by  INTEGER, requested_name TEXT NOT NULL, requested_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    approved_by   INTEGER, approved_name TEXT, decided_at TIMESTAMPTZ,
    status        TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','approved','rejected','expired','executed','failed')),
    result        JSONB, trace_id UUID NOT NULL, label TEXT NOT NULL
);
```

The agent receives `{status: "pending_approval", pending_id, label}` and
can poll `pending_status(id)`. An approver (tier ≥4, and `03 §5.3` rules)
approves in the UI or via `pending_approve(id)`; the gateway then forwards
the call **under the requester's frozen grant** and stores the result for
`pending_result(id)`. This is the compute equivalent of vault promotion,
and it is the one place argument *values* are stored — by design, for the
approver, with the row governance-flagged.

## 6. Reference upstream profiles

Shipped as `profiles/*.json` and selectable in the connection form; each
is a `config` template with guards for a known upstream server's tool
names. Profiles are data, not code; an admin edits them.

| Profile | Kind | Guarded arguments |
|---|---|---|
| `github-mcp` | code | `owner/repo` → `repos`; write tools require `write`; delete/force require tier 5 |
| `gitea-mcp` | code | as above |
| `local-git-mcp` | code | `path` → `repos` (repo roots as globs) |
| `ssh-mcp` | compute | `host` → `hosts`; `command` → `commands`; `path` → `paths`; `restart`/`deploy` labelled approvals |
| `gdrive-mcp` | documents | `folderId`/`fileId` → `folders` (file ids resolved to their ancestor folders via the upstream's `get_file` and cached 10 min); write tools require `write`; `share_*` require `share` |

Resolving a Drive file id to its folder is the one guard that needs an
extra upstream call; it is cached and its failure is a deny.

## 7. What the agent sees

For `datum-main` with the grant in `19 §1` plus:

```json
"memory": {"read": ["org/**","team/geo/**","agents/datum-main/**"], "write": ["team/geo/**","agents/datum-main/**"]},
"code": {"connections": ["github"], "repos": ["datum/*"], "write": true, "tools": {"allow": ["*"], "deny": ["delete_*"]}},
"compute": {"connections": ["vm-lan"], "hosts": ["vm102"], "commands": {"allow": ["^(ls|cat|tail|systemctl status|journalctl)\\b"], "deny": ["\\bsudo\\b"]}, "paths": {"read": ["/home/ubuntu/**"], "write": []}, "approval_required": ["restart"]},
"documents": {"connections": ["gdrive"], "folders": ["1AbC…"], "write": false, "share": false}
```

`tools/list` returns, in one list: `SCIMAC__*`, `Testing__*`, `vault_*`,
`quarantine_promote`, `proxy_request`, `delegate_*`, `job_*`, `memory_*`,
`gh__get_file_contents`, `gh__push_files`, `gh__create_pull_request` (but
not `gh__delete_repository`), `vm__run_command`, `vm__read_file`,
`vm__restart_service` (listed; approval-gated), `drive__search`,
`drive__get_file` (no `drive__share_file`).

Onboarding a new agent is: one principal, one grant, one token, one URL.
That is the product.

## 8. Guards to register (FED)

| id | Guard |
|---|---|
| FED-001 | federated tools listed only for connections in the grant block |
| FED-002 | tool allow/deny filter applied to the catalogue |
| FED-003 | `default: deny` hides unguarded tools |
| FED-004 | argument guard: value not matching grant glob → denied before forwarding |
| FED-005 | argument guard: missing guarded argument → denied |
| FED-006 | `requires.write` enforced |
| FED-007 | `requires.tier` enforced |
| FED-008 | compute: deny regex checked before allow |
| FED-009 | approval-gated call is not forwarded until approved |
| FED-010 | approved call runs under the requester's frozen grant |
| FED-011 | upstream auth never reaches the agent (result headers/meta scrubbed) |
| FED-012 | upstream unavailable → omitted + audit, not an error |
| FED-013 | argument values (other than guarded keys) never in audit |
| FED-014 | Drive file-id → folder resolution failure is a deny |
| FED-015 | child `code/compute/documents` blocks must narrow the parent's |
| MEM-001 | namespace deny wins |
| MEM-002 | `org/**` writes require policy tier |
| MEM-003 | every write produces a history row |
| MEM-004 | `expect_version` mismatch → 409 |
| MEM-005 | search results filtered by readable namespaces |
| MEM-006 | bodies never in audit |
