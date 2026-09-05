# Datum-Gate — Design Corpus

This is a complete, buildable specification for a system that solves the same
problem as Datum-Sync and operates the same way from the outside: a
**multi-tenant agent gateway** where humans and AI agents authenticate the
same way, hold tiered authority over repositories, credentials and vault paths,
and run work through one orchestration layer that contains no model.

The product is an **organisation-level agent control plane**: agents are barebones standalone, and once registered and authorised through the gateway they gain scoped, audited access to the company's shared memory, codebases, VMs and Google Drive, alongside published workspaces and the vault (`21-federation-and-core-resources.md`).

It is written for an implementing agent. Every document is self-contained
enough to build its component from, and cross-references the others by file
name. Nothing here is a critique of the original; where this design makes a
different choice, `20-decision-log.md` records what was chosen and why, so the
implementer is never guessing at intent.

Working name: **Datum-Gate**. Python package: `datumgate`. Rename freely — the
name appears only in `16-config-deploy.md` and the package path.

## Fixed constraints

| Constraint | Value |
|---|---|
| Language | Python 3.12 |
| Web framework | FastAPI + uvicorn |
| Database | PostgreSQL 16 + PostGIS, accessed via asyncpg, **no ORM** |
| Queue / events | Postgres tables + `pg_notify` — no broker |
| Front end | Vanilla JS ES modules, no build step, no CDN, no `innerHTML` |
| Agent protocol | MCP Streamable HTTP, JSON-RPC 2.0, protocol `2025-06-18` |
| Agent auth | Bearer tokens; OAuth 2.1 + PKCE for interactive MCP clients |
| Workspace runtime | Python subprocess. No FME. (A future visual workflow builder is a separate project and is out of scope; see `05-workspace-contract.md §9` for the seam it plugs into.) |
| Secrets | AES-256-GCM, key from environment, connection name as AAD |

## Reading order

Build in the order of `18-build-plan.md`. Read in this order first:

1. `01-vision.md` — what the system is, is not, and why it is shaped this way
2. `02-domain-model.md` — the nouns, and how they relate
3. `03-authority.md` — principals, credentials, grants, tiers, delegation
4. `04-schema.md` — the full DDL
5. `05-workspace-contract.md` — what a workspace is and how it is published
6. `06-job-engine.md` — how work runs
7. `07-connections-and-proxy.md` — the credential store and credential proxy
8. `08-vault-gate.md` — scoped filesystem access
9. `09-schedules-automations.md` — time and event triggers, durable deliveries
10. `10-rest-api.md` — every HTTP endpoint
11. `11-mcp.md` — the MCP surface
12. `12-oauth.md` — the authorization server
13. `13-hosted-services.md` — what a job leaves running
14. `14-audit.md` — the single audit spine
15. `15-web-ui.md` — screens and front-end rules
16. `16-config-deploy.md` — environment, systemd, ops
17. `17-security-guards.md` — the guard registry and mutation harness
18. `18-build-plan.md` — phased build with acceptance gates
19. `19-reference-scenarios.md` — end-to-end scenarios the build must satisfy
20. `20-decision-log.md` — decisions and rationale
21. `21-federation-and-core-resources.md` — upstream MCP federation, argument guards, and the four core company resources (memory, code, compute, documents)
22. `22-agent-lifecycle.md` — registration, approval, review, restriction, retirement
23. `23-tenancy-and-review.md` — teams, activity summaries, anomaly signals, the review queue

24. `24-future-improvements.md` — functionality beyond the build, with where each attaches and what it costs; not part of any build step

**Read 21–23 immediately after 01.** They carry the org-level vision: an agent is barebones on its own and becomes capable by connecting to the gateway. 03–20 are the machinery that makes that safe.

## Conventions used in this corpus

- **MUST / MUST NOT / SHOULD** carry RFC 2119 meaning.
- SQL is PostgreSQL 16. Column types are explicit. Every table has a comment
  block explaining its reason for existing.
- JSON documents that live in JSONB columns have a schema in the document
  that owns them and a Pydantic model name in `datumgate/models.py`.
- Every error crossing HTTP uses the envelope in `10-rest-api.md §2`,
  except OAuth endpoints (RFC 6749 shape) and JSON-RPC (MCP shape).
- Identifiers: tables `snake_case` plural; Python modules `snake_case`;
  JSON keys `snake_case`; MCP tool names `[A-Za-z0-9_-]`.
- Times are `TIMESTAMPTZ` in the database, ISO-8601 with offset on the wire,
  UTC internally. The default *display* timezone is a config value
  (`DEFAULT_TIMEZONE`, default `Pacific/Auckland`).
- "Principal" means any authenticated caller, human or agent. "Actor" is the
  principal as recorded in the audit log.

## Module map (target layout)

```
datumgate/
  __init__.py
  config.py           env + policy.yaml loading, path helpers
  db.py               pool, JSON codecs, transaction helper
  errors.py           ApiError + envelope + handlers
  models.py           Pydantic models for every JSONB document
  audit.py            the audit spine (write-only API)
  authority/
    grants.py         Grant model, narrows(), intersect(), tier semantics
    credentials.py    mint/hash/resolve every credential kind
    principals.py     CRUD, delegation checks
    resolve.py        request -> Principal (bearer, cookie)
    passwords.py      argon2 + lockout
  catalogue/
    manifest.py       manifest schema, param coercion
    repository.py     disk walk, content hashing
    publish.py        publish gate, versioning
  engine/
    jobs.py           submit/claim/lease/finish/cancel
    worker.py         process loop, leases, reaper
    runner.py         subprocess supervisor
    child.py          in-subprocess harness
    events.py         SSE from job_log + notify
    execute.py        run_sync helper for stream/download/mcp
    uploads.py        FILE parameter intake
    artifacts.py      path resolution, retention
  connections/
    store.py          CRUD, config/secret split
    crypto.py         AES-GCM with key ids
    proxy.py          credential proxy + SSRF guard
    testers.py        per-type connection tests
  memory/
    store.py          namespaces, entries, history, search
    tools.py          MCP + REST projections
  federation/
    catalogue.py      upstream tools/list cache and name mapping
    guards.py         argument guards, approval gating
    client.py         upstream MCP client with auth injection
    profiles/         github-mcp.json gitea-mcp.json local-git-mcp.json ssh-mcp.json gdrive-mcp.json
  lifecycle/
    registration.py   codes, self-register, claim, approve
    review.py         rollups, signals, review queue
  vault/
    paths.py          normalise, glob, permits
    scope.py          coherence validation
    fs.py             read/write/list
    quarantine.py     quarantine + promotions
  triggers/
    schedules.py
    automations.py    parse, match, plan
    deliveries.py     outbox claim/execute/retry
    egress.py         public-address check, redirect-safe fetch
  surfaces/
    rest.py           /rest/v1
    service_paths.py  /stream /download /upload
    mcp.py            /mcp
    oauth.py          OAuth 2.1 server
    serve.py          /serve hosted services
    ui.py             shell + static mount + sign-in
  cli/
    __main__.py       `python -m datumgate <cmd>`
    principals.py     create/token/passwd/disable/list
    migrate.py
    sync.py           repository sync
    keys.py           secret key generation / rotation
  static/             web UI
migrations/           NNN_name.sql, applied in order
policy.yaml           governance paths, promote policy, retention
tests/
  guards.yaml         guard registry (17-security-guards.md)
  break_the_guard.py
  ...
```
