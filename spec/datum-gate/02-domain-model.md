# 02 — Domain Model

## 1. Entities

```
                 ┌──────────────┐  parent_id   ┌──────────────┐
                 │  principal   │◄─────────────┤  principal   │  (delegation tree)
                 └──────┬───────┘              └──────────────┘
                        │ 1..n
                 ┌──────▼───────┐
                 │  credential  │  token | password | session | oauth_access | oauth_refresh
                 └──────────────┘

┌────────────┐ 1..n ┌────────────┐ 1..n ┌────────────────────┐
│ repository ├──────┤ workspace  ├──────┤ workspace_version  │  (immutable, hash-addressed)
└────────────┘      └─────┬──────┘      └─────────┬──────────┘
                          │ current_version_id     │
                          └─────────────┐          │
                                        ▼          ▼
                                   ┌─────────────────┐ 1..n ┌──────────┐
                                   │       job       ├──────┤ job_log  │
                                   └───┬─────────┬───┘      └──────────┘
                    artifacts on disk  │         │ source_job
                                       │         ▼
                                       │   ┌────────────────┐
                                       │   │ hosted_service │  /serve/{name}/
                                       │   └────────────────┘
                                       ▼
                              ┌────────────────┐
                              │ automation_run ├──1..n──► delivery (outbox)
                              └────────────────┘

┌────────────┐      ┌──────────────┐      ┌─────────────┐      ┌────────────┐
│ connection │      │  schedule    │      │ automation  │      │ promotion  │  (vault quarantine → shared)
└────────────┘      └──────────────┘      └─────────────┘      └────────────┘

┌────────────┐      ┌──────────────┐      ┌─────────────┐
│ audit_log  │      │ oauth_client │      │  worker     │  (lease registry)
└────────────┘      └──────────────┘      └─────────────┘
```

## 2. Glossary

**Principal.** Any authenticated identity. Has a `kind` (`human`, `agent`,
`system`), a single **grant** document, optional `parent_id`. Everything that
records "who" records a principal id and its name at the time.

**Credential.** A way a principal proves itself. One table, five kinds.
Only a hash is stored for token-shaped kinds; argon2 for passwords. A
credential is always bound to exactly one principal and (for OAuth) one
client and one audience.

**Grant.** A JSON document declaring authority: tier, repository scope,
connection usage and proxy rights, vault scope, and limits. The only
permission model in the system. See `03-authority.md`.

**Tier.** An integer 1–5 on the grant that sets which *verbs* a principal
may use. Connections also carry a tier — the sensitivity of the credential —
and a principal may only use connections at or below its own tier.

**Effective grant.** The intersection of a principal's own grant with every
ancestor's grant, computed at authentication. What every check consults.

**Repository.** A named directory of workspaces on disk, mirrored by a row.
The unit of scope in a grant.

**Workspace.** A directory with `main.py`, `manifest.json` and `MANIFEST.md`.
Callable only when it has a **current version**.

**Workspace version.** An immutable record of a published manifest plus a
content hash of the workspace directory at publish time. Jobs pin a version.
Publishing produces a new version; the workspace's `current_version_id`
moves only if the gate passes.

**Manifest.** The workspace's declared interface: parameters, outputs,
services it exposes, connections it needs, timeout. Stored verbatim in the
version row.

**Job.** One run of one workspace version with one parameter set, under one
frozen effective grant. Lifecycle `queued → running → complete|failed|cancelled`.
Held by a **lease** while running.

**Artifact.** A file or directory a job wrote into its own artifact
directory, described in `jobs.artifacts` and reconciled against the manifest's
declared outputs.

**Hosted service.** A `service/*` artifact that stays reachable at
`/serve/{name}/` after its job ends, or a reverse-proxied origin registered
by an admin.

**Connection.** A named, typed, tiered, scoped credential: a readable
`config` half and a sealed `secret` half.

**Credential proxy.** An MCP tool / REST endpoint that forwards an HTTP
request through an `http` connection, injecting its secret server-side.

**Vault.** A filesystem root (`VAULT_PATH`) that principals reach only
through scoped operations: read, write, list, quarantine-write, promote.

**Vault scope.** The `vault` block of a grant: glob lists per action plus
`deny`. Deny always wins.

**Quarantine / promotion.** Writes by lower-trust principals land in
quarantine paths; moving content from quarantine to a shared path is a
**promotion**, which may require a second principal under policy.

**Schedule.** A cron or interval trigger that submits a job. Its due time is
a column the worker polls.

**Automation.** A YAML document: one trigger, an ordered list of actions.
Firing creates an **automation run** and one **delivery** per action.

**Delivery.** One row in the outbox: an action to perform, with a dedupe
key, attempts, and next-attempt time. At-least-once, deduplicated.

**Worker.** A process that leases jobs and deliveries, ticks schedules, and
heartbeats its row. Several may run.

**Audit log.** One append-only table receiving a row for every authorised
action on every surface, keyed by a server-generated trace id.

**Trace id.** A UUID minted per inbound request by the gateway. Propagated
into jobs, deliveries and audit rows. Clients may send `X-Trace-Id`; it is
stored separately as `client_trace_id` and never trusted.

**Policy.** A YAML file loaded at startup declaring governance paths, the
promotion rule, retention windows and default limits.

## 3. Naming and identity rules

| Thing | Rule |
|---|---|
| Principal name | `^[a-z][a-z0-9._-]{1,62}$`, unique, immutable after creation |
| Repository name | `^[A-Za-z][A-Za-z0-9_-]{0,63}$`, must equal the directory name |
| Workspace name | same regex; must equal the directory name and `manifest.name` |
| Connection name | `^[A-Za-z][A-Za-z0-9_.-]{0,63}$`, globally unique |
| Hosted service name | `^[a-z0-9][a-z0-9-]{0,62}$`, globally unique — it is a URL segment |
| MCP tool name | `{repository}__{workspace}` with unsafe chars replaced by `_`, ≤128 chars |
| Vault path | relative, `/`-separated, no `.`/`..` segments, no `\`, no NUL, ≤1024 chars |
| Schedule / automation name | `^[a-z0-9][a-z0-9-]{0,62}$`, unique per kind |

## 4. Ownership and cascade summary

| Deleting… | Effect |
|---|---|
| a principal | credentials cascade; children are **disabled**, not deleted (they may hold audit history); jobs/audit rows keep `actor_name` text and a dangling id |
| a repository | workspaces and versions cascade; jobs keep denormalised names; hosted services owned by its workspaces are deleted |
| a workspace version | refused if it is the current version or any job pins it and is not terminal |
| a job | artifact directory removed by the retention sweeper only; hosted services `source_job` set NULL, not removed |
| a connection | refused if any current workspace version declares it; otherwise deleted, audit rows keep the name |
| an automation | runs and pending deliveries cascade |
