# 14 — The Audit Spine

One append-only table, `audit_log`, receives a row for every authorised
action on every surface. `job_log` (workspace output) and
`automation_runs`/`deliveries` (action outcomes) are separate because they
are high-volume and have their own shape, but every one of those rows
carries the `trace_id` that joins it to the audit spine.

## 1. What a row is

| Column | Content |
|---|---|
| `trace_id` | server-minted per inbound request (or per worker action); the join key |
| `client_trace_id` | `X-Trace-Id` as sent; untrusted, never used for auth or dedupe |
| `actor_id`, `actor_name`, `actor_kind` | the principal; name and kind copied so the row outlives the principal |
| `via` | `rest mcp ui cli worker serve oauth` |
| `verb` | vocabulary, §3 |
| `target_kind`, `target` | a safe identifier — a job id, `repo/ws`, a vault path, a connection name, a principal name. **Never content.** |
| `outcome` | `ok` — done; `denied` — refused by auth/scope/tier/policy; `error` — attempted and failed |
| `error_code` | the envelope code or JSON-RPC code |
| `duration_ms` | handler time |
| `governance` | true when the target matches `policy.governance_paths` or the verb is in `policy.governance_verbs` (principal and grant changes, key operations, policy reload, proxy-service registration, promotion approval) |
| `detail` | small JSON: status codes, counts, changed field *names* (not values), the `_wait_seconds` used, etc. Bounded to 2 KiB; the writer truncates and sets `detail.truncated = true`. |

What is **never** stored: request or response bodies, vault content, secrets,
parameter values other than their names, password attempts, raw tokens or
hashes.

## 2. Writer

`audit.write(row)` is fire-and-forget through an in-process bounded queue
(`AUDIT_QUEUE_MAX` 10 000) drained by a background task in batches of ≤200
with `COPY`/multi-row insert. If the queue is full the writer drops the row
and increments a counter exposed on `/health` as `audit_dropped` — an
audit gap is reported, never silent. The worker uses the same module with
its own queue.

`audit.write_sync(row)` exists for the handful of places where the audit
row must land before the response (denied outcomes on governance targets,
principal/grant changes, key operations): it awaits the insert.

## 3. Verb vocabulary

`{kind}.{action}`; each handler names its own:

```
auth.signin auth.signout auth.token.mint auth.token.revoke auth.password.set
auth.lockout auth.oauth.register auth.oauth.authorize auth.oauth.token auth.oauth.revoke
principal.create principal.update principal.disable principal.enable principal.delete
principal.delegate principal.delegate.revoke
workspace.sync workspace.publish workspace.unchanged workspace.withdraw workspace.activate
job.submit job.claim job.finish job.cancel job.resubmit job.reap job.artifact.read
upload.create
connection.create connection.update connection.delete connection.test connection.resolve
proxy.request
vault.read vault.list vault.stat vault.search vault.write vault.delete
vault.quarantine vault.promote.request vault.promote.approve vault.promote.reject vault.promote.apply vault.promote.expire
schedule.create schedule.update schedule.delete schedule.fire schedule.run_now
automation.create automation.update automation.toggle automation.delete automation.fire
delivery.done delivery.retry delivery.dead
webhook.receive
service.register service.update service.delete serve.read serve.proxy
mcp.initialize mcp.tools.list mcp.tools.call mcp.resources.list mcp.resources.read
policy.reload keys.reseal
retention.sweep
```

MCP `tools/call` writes **two** rows when the tool is a workspace: one
`mcp.tools.call` with `target=tool_name`, and the `job.submit` it caused —
both under the same trace. Built-in tools write `mcp.tools.call` plus the
underlying verb (`vault.read`, `proxy.request`).

## 4. Reading

`GET /rest/v1/audit` and `GET /rest/v1/audit/trace/{id}` (`10 §13`). Row
visibility:

- tier 1–3: own rows (`actor_id = self`)
- tier 4: rows whose target is in scope (jobs/workspaces/services by
  repository; vault by the caller's read scope; principals by the
  delegation subtree; connections by usability) plus own rows
- tier 5: everything

The trace view joins `audit_log`, `jobs`, `job_log` (first/last 20 lines),
`automation_runs` and `deliveries` on `trace_id` and orders by time, which
is the answer to "what happened when the agent asked for X".

## 5. Retention

`policy.retention.audit_days` (default 365). The sweeper deletes older
rows **except** `governance = true` rows, which are kept for
`policy.retention.governance_audit_days` (default 0 = forever). Deletion is
itself audited (`retention.sweep`, count in detail).

## 6. Export

`python -m datumgate audit export --since --until --format jsonl|csv`
streams rows; intended for shipping to an external SIEM. No HTTP export
endpoint: the CLI is the trust boundary for bulk extraction.
