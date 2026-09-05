# 08 — Vault Gate

The vault is a directory tree (`VAULT_PATH`, typically an NFS mount shared
with agent hosts) that principals reach only through the gateway. The gate
enforces the `vault` block of the effective grant, audits every operation,
and mediates the quarantine → shared promotion flow.

## 1. Paths

A vault path is relative to the root, `/`-separated, and normalised by
`vault/paths.normalise()`:

- must be a non-empty string ≤ 1024 chars
- no NUL, no `\`
- must not start with `/`
- trailing `/` stripped
- no segment may be `.`, `..` or empty
- **traversal is refused, not resolved** — a path that needs resolving is a
  400, because the written form is what the scope is checked against

The absolute filesystem path is `VAULT_PATH / normalised`, and after
building it the gate additionally asserts `resolved.is_relative_to(VAULT_PATH.resolve())`
to defeat symlinks that point outside the root (a symlink inside the vault
pointing out is treated as not existing).

## 2. Glob semantics

| Token | Matches |
|---|---|
| `*` | any run of non-`/` characters within one segment |
| `**` | zero or more whole segments (`dev/**` matches `dev`, `dev/a`, `dev/a/b`) |
| `?` | one non-`/` character |
| anything else | literal |

A pattern segment may be `*`, `**`, or a literal possibly containing `?`.
Partial wildcards (`*foo`, `f*o`) are refused at grant validation — the
engine handles them correctly but operators do not expect them.

Compiled once per pattern with an LRU cache; match is anchored at both ends.

## 3. Decision

```
permits(scope, action, path):
    if action not in {read, write, quarantine}: raise
    if scope is None or empty: return False
    if any(match(p, path) for p in scope.deny): return False
    return any(match(p, path) for p in scope[action])

permits_promote(scope, src, dst):
    if deny matches src or dst: return False
    return any(match(edge.src, src) and match(edge.dst, dst) for edge in scope.promote)
```

Rules that hold everywhere:

- **Deny wins**, checked first, ends the question.
- **Allows are literal.** `write` does not imply `read` here. The coherence
  validator (§4) requires write ⊆ read at grant-write time; inferring it at
  decision time would let the validator be deleted with every test passing.
- `list` on a directory is permitted if the caller's `read` scope could
  match anything beneath it (prefix reasoning on the literal prefix of each
  read pattern, or a leading `**`); entries are then filtered
  individually. A directory nothing could match beneath returns **403**, not
  an empty list — an empty list leaks existence.
- A refused path returns 403 and never 404. The existence of an
  out-of-scope path is not the caller's business.

## 4. Grant coherence (write time)

Run by `vault/scope.validate()` from `Grant` validation (`03 §3`):

1. keys ⊆ `{read, write, quarantine, promote, deny}`; each a list of
   non-empty strings; promote entries are `"src -> dst"` with exactly one
   ` -> `
2. every pattern passes the segment rules (§2), no `..`/`.` segments
3. write-implies-read: every `write` pattern is subsumed by some `read`
   pattern (`03 §6`)
4. every promote edge's `src` is subsumed by some `quarantine` **or**
   `write` pattern; its `dst` is subsumed by some `write` pattern
5. no `deny` pattern equals any allow pattern; and no `deny` pattern
   subsumes *every* pattern of any non-empty allow list (a dead action)
6. tier rules from `03 §3` (tier 1: read/list only; tier 2: + quarantine;
   tier ≥3: write; promote request needs tier ≥3; approval needs tier ≥4)

Error messages name the key and the offending pattern.

## 5. Operations

All operations: normalise → authorise → touch disk → audit. Every operation
writes one `audit_log` row with `target_kind='vault'`, `target=<path>`, and
`governance` set by `policy.governance_paths` (§7).

| Operation | Grant action | Effect | Limits |
|---|---|---|---|
| `read(path)` | `read` | returns UTF-8 text (bytes decoded with replacement) up to `VAULT_MAX_READ_BYTES` (256 KiB), with `truncated` and `size`; `offset`/`limit` params for paging | — |
| `write(path, content, mode)` | `write` | `mode` = `create` (fails if exists), `overwrite`, `append`; creates parents; writes to a temp file then `os.replace` (atomic); returns new size and sha256 | content ≤ `VAULT_MAX_WRITE_BYTES` (4 MiB) |
| `delete(path)` | `write` | removes a file; directories only when empty | — |
| `list(path, depth)` | `read` (prefix) | entries with `name, kind, size, mtime`, filtered by scope; `depth` 1..3 | ≤ 2000 entries |
| `stat(path)` | `read` | size, mtime, sha256 | — |
| `quarantine_write(path, content)` | `quarantine` | as `write`, mode `create` only, path must match a `quarantine` pattern; sets an xattr/sidecar `path.dgq.json` recording `{written_by, trace_id, sha256, at}` | as write |
| `promote(source, destination, reason)` | `promote` edge | see §6 | — |
| `search(query, roots, limit)` | `read` | filename and content substring search restricted to paths the caller may read; a pluggable backend (§8) | ≤ 200 results |

MCP exposes these as `vault_read`, `vault_write`, `vault_delete`,
`vault_list`, `vault_stat`, `quarantine_write`, `quarantine_promote`,
`vault_search`; REST exposes them under `/rest/v1/vault/…` (`10 §9`).

Concurrency: `write`/`promote` take a per-path advisory lock
(`pg_advisory_xact_lock(hashtext(path))`) for the duration of the filesystem
operation so two gateway processes cannot interleave a write and a promote
on the same path.

## 6. Quarantine and promotion

Lower-trust principals (tier 2 agents, drones) write only into quarantine
patterns. Moving content from quarantine into a shared path is a
**promotion** and always produces a `promotions` row.

```
promote(src, dst, reason):
  normalise both; permits_promote(scope, src, dst) else 403
  src must exist and be a file; dst must not exist unless policy.promote.allow_overwrite
  hash = sha256(src)
  if policy.promote.require_second_principal and caller.tier < policy.promote.self_approve_tier (default 5):
      INSERT promotions(status='pending', content_hash=hash, …)
      return {"promotion_id": …, "status": "pending"}
  else:
      _apply(src, dst, hash) ; INSERT promotions(status='done', approved_by=caller)
      return {"status": "done"}

approve(promotion_id):
  caller.tier >= policy.promote.approver_tier (default 4)
  caller.id != requested_by  (unless caller.tier >= self_approve_tier)
  permits_promote(caller.scope, src, dst) — the approver must also hold the edge
  re-hash src; if != content_hash: status='rejected', reason='source changed since request'
  _apply ; status='done', approved_by=caller

reject(promotion_id, reason): approver tier; status='rejected'
expire: worker sweeps pending rows older than policy.promote.pending_ttl_hours → 'expired'

_apply(src, dst, hash):
  copy to dst via temp + os.replace; remove src and its sidecar;
  write dst sidecar {promoted_from, promotion_id, approved_by, at}
```

Every step audits `vault.promote.request|approve|reject|apply`.

## 7. Governance paths

`policy.yaml`:

```yaml
governance_paths:
  - "SOUL.md"
  - "skills/**"
  - "hooks/**"
  - "policy/**"
```

Any vault operation whose normalised target matches a governance pattern is
audited with `governance: true`. Additionally, `policy.governance_write_min_tier`
(default 4) is enforced: a write, delete or promotion *into* a governance
path requires that tier regardless of the grant — the grant says where you
may write; policy says what is protected. The two are checked in that
order, and both are audited on refusal.

## 8. Search backend

`vault/fs.search()` calls a backend selected by `policy.vault.search_backend`:

- `filesystem` (default): walks readable roots, filename glob + content
  substring, bounded by `limit` and a 5s budget.
- `external`: an `http` connection named by `policy.vault.search_connection`
  is called with `{"query","roots","limit"}` and must return
  `[{"path","snippet","score"}]`; results are **re-filtered by the caller's
  read scope** before return, so an index that knows more than the caller
  may see cannot leak through it.

The graph tools in the original proposal (backlinks, tags, notes) are the
`external` case: an index service exposes them, the gate filters by scope.
No cross-database FDW is required by the gateway.

## 9. Delegated scope on jobs

A workspace run may touch the vault only through the connection type
`file` with `root` inside `VAULT_PATH` — and that connection is resolved
against the job's **frozen effective grant** (`06 §2`): the child harness's
`clients.vault(conn_obj)` helper applies `permits()` with
`effective_grant.vault` before every operation. A drone's job therefore
cannot reach beyond the slice its dispatcher gave it, even if the drone's
own principal is later widened.
