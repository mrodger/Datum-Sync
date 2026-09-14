# Incremental integrations

The auth plane is the public gateway. Legacy capabilities move behind it one bounded provider at a time, with explicit grants, tests and visible audit records. The legacy main server is not exposed wholesale.

## Increment 1 — MCP metadata adapter (implemented)

The process manager starts two loopback services:

| Service | Address | Purpose |
|---|---|---|
| Auth plane and MCP gateway | `127.0.0.1:8210` | Agent credentials, sessions, tool filtering, calls and audit |
| Legacy MCP adapter | `127.0.0.1:8211` | Read-only access to repository/workspace definitions in the v1 registry |

The gateway connects to the adapter with an independently generated service token. That token is stored as an encrypted, versioned managed credential and is never returned by the API. An agent's inbound bearer token is not forwarded upstream.

The adapter currently publishes:

- `legacy__list_repositories`
- `legacy__list_workspaces`
- `legacy__workspace_manifest`

These inspect the repository registry and report whether each item is published in the local legacy database. They do not execute a workspace, read vault files, invoke the HTTP credential proxy, start a job, or launch a subprocess. Existing agents receive no new access. The enrolment template names these metadata tools explicitly for newly enrolled demo agents; removing a tool from a principal grant removes it from `tools/list` and makes calls indistinguishable from an unknown tool.

The **Secrets & Proxies** screen shows adapter health, the persistent discovered catalogue and credential-access state. The **Access lab** can call the legacy tools through a normal credential-bound gateway MCP session.

## Increment 2 — scoped job observability (implemented)

Six read-only job tools are projected through the same connection:

- `legacy__job_summary`
- `legacy__list_jobs`
- `legacy__job_status`
- `legacy__job_log`
- `legacy__job_events`
- `legacy__job_artifacts`

The gateway constructs the upstream principal context from the authenticated credential. Caller arguments cannot replace it. Effective repository grants are intersected through the principal ancestry and applied to repository/workspace metadata and every job query.

New jobs store the stable portal principal ID for direct API, MCP, schedule and automation submission paths. The adapter verifies that principal is active, expands its descendant IDs, and uses the immutable ID for ownership. The submitter name remains useful display and audit data. Rows created before this migration retain a name-based compatibility fallback only when their principal ID is null. Unknown jobs, jobs owned by another principal and jobs outside repository scope return the same error.

Status responses omit job parameters and artifact contents. Artifact reads return only `name`, `type`, `primary`, `dir` and `size`; internal filenames and paths stay private. Logs and durable status/progress/log events use forward-only `after_id` paging with at most 200 entries. Federated error and log messages are capped at 4 KiB, and the gateway retains its 1 MiB upstream response limit. State changes and their event rows commit in one database transaction. Calls are recorded by tool and connection without copying log text into the audit trail.

This increment does not download artifacts, submit, cancel or resubmit jobs through the auth portal.

## Increment 3 — deterministic MCP flow logging (implemented)

Every portal `POST /mcp` request now creates a server-trace flow summary and an append-only event timeline. The timeline records receipt, JSON-RPC parsing, authentication, session admission or validation, catalogue access, tool authorization, local or federated dispatch, upstream completion and the final outcome. Invalid bearer attempts remain anonymous; a valid principal rejected for a missing or expired session remains attributable.

The flow summary records safe operational dimensions: principal and credential identifiers, MCP session and client, method, tool, provider, connection, upstream tool, outcome, normalized error code, byte counts and duration. It never stores bearer credentials, headers, tool arguments, result bodies or upstream content. Event notifications contain only event ID, trace ID and kind; PostgreSQL remains the durable source.

The operator-only **MCP Activity** screen lists recent flows and expands each one into its ordered event timeline. The response `X-Trace-Id` is the same server-generated ID used by the durable flow and downstream federation records. Logging failures do not alter MCP results and are counted as `mcp_audit_dropped` on the health endpoint.

## Increment 4 — managed MCP credential access (implemented)

The gateway now separates an agent's authority from the upstream service identity. The seeded `legacy-local` identity has immutable encrypted versions, status and expiry metadata. The current version is decrypted only inside the federation process after all agent, ancestry, tier, policy, grant and credential checks pass. Inbound `Authorization`, cookies and hop-by-hop headers are discarded before the configured service authentication is injected.

Agents see a safe catalogue of logical credentials and exact tools they may request. They submit a purpose and bounded lifetime. The owning operator can deny the request or approve a subset of its tools and duration after the server rechecks current authority. **Secrets & Proxies** shows credentials, unique effective agents, each grant's effective state, expiry and lifecycle events. Operators can revoke one grant or disable a service identity globally; the next tool listing and call use live database state.

Rotation stages a new encrypted version, probes it with MCP `initialize` and `tools/list`, and activates it transactionally only after success. A failed candidate is retained as failed metadata while the old version stays active. Responses and events do not contain candidate values or raw upstream errors. See [docs/review/CREDENTIAL_ACCESS_REVIEW.md](../review/CREDENTIAL_ACCESS_REVIEW.md) for the requirement assessment and production gaps.

This increment introduced the seeded loopback identity. Increment 5 applies the same model to vetted provider templates. Generic connection onboarding, external key management and complete multi-operator tenancy remain future work.

## Increment 5 — governed MCP server registry and provider demos (implemented)

The operator can register vetted local provider templates from **Secrets & Proxies**. Registration creates an operator-owned server record and an encrypted service identity, then discovers the upstream MCP catalogue. Discovered tools keep stable enablement and minimum-tier controls across refreshes. Operators can disable a whole server or individual tool globally, or remove one tool from one agent's credential grant. Agent catalogues and calls evaluate those changes from live state. Server registration, catalogue refresh/failure, server state changes and tool state changes have a separate connection-event trail.

The **OfficeCLI document demo** follows OfficeCLI's one-command-tool shape. Its provider accepts only `list`, `view NAME text` and `validate NAME` for three in-memory sample documents; edit, delete, batch and arbitrary commands are rejected. This demonstrates how a broad command-oriented MCP interface should be narrowed at the provider boundary. It is compatibility demo code, not the upstream OfficeCLI executable. See the [OfficeCLI MCP command documentation](https://github.com/iOfficeAI/OfficeCLI/wiki/command-mcp).

The **PostgreSQL orders demo** exposes five structured tools over a real local `mcp_demo.orders` table: schema and table discovery, one table description, bounded samples and grouped summaries. It accepts no SQL, fixes the exposed schema/table and parameterizes the only user filter. A production deployment of Microsoft's server should also use a database role limited to the intended databases and privileges; see the [Microsoft PostgreSQL MCP usage guide](https://github.com/microsoft/postgres-mcp/blob/main/USAGE.md).

Every connection belongs to an operator. Credential catalogues, requests, approvals, grants, lifecycle actions and the operator server inventory apply that ownership boundary. This is the expansion seam for multiple operator fleets. Full tenancy still needs tenant identifiers and scoping on principals, sessions, OAuth records, resource data, flows and general audit queries, plus tenant-aware uniqueness and operator roles.

The current registration API deliberately accepts only vetted loopback templates. Generic URLs and stdio processes are not accepted. Supporting installed OfficeCLI or Microsoft PostgreSQL MCP packages next requires a supervised stdio-to-HTTP bridge, process isolation, explicit executable/config allowlists, bounded I/O and lifecycle management.

## Increment 6 — curated Datum Harness tools (implemented)

The **Datum Harness tools** provider adds three actual harness capabilities behind the same server, credential, agent-grant and audit controls:

- harness__list_personas loads the seven persona definitions from demo/personas.py.
- harness__render_leaflet_map adapts the harness Leaflet tool with fixed basemaps, validated coordinates, at most 100 markers, escaped popup text and no filesystem write.
- harness__worms_lookup adapts the harness WoRMS utility into a bounded asynchronous call. It accepts 1–20 scientific names, sends those names to the public [WoRMS REST API](https://www.marinespecies.org/rest/), caps the upstream response, and returns selected taxonomy fields.

This is a curated boundary around runnable tools, rather than exposing datum_mcp wholesale. The full harness server currently assumes a private vault, PostgreSQL orchestrator database, LAN services, Claude skill directories and optional geospatial packages. Its dynamic tool creation executes submitted Python, and its CUA/desktop tools can observe or control a graphical session. Pipeline execution also carries side effects and contains machine-specific paths. Those surfaces require separate provider identities, tiers, resource policies, job records and process isolation before registration.

## Suggested next increments

1. Add bounded artifact downloads with explicit content-type, size and retention rules.
2. Add workspace execution at tier 3 with human elevation, recorded submitter identity and resource limits.
3. Add cancel/resubmit as separate tier-3 tools with current-authority checks.
4. Add selected HTTP proxy connections at tier 3 using the managed credential grant model and argument policy.
5. Add supervised stdio providers with provider-specific credential validation, process isolation and upstream session handling.
6. Generalize registration beyond vetted templates after adding destination policy and tenant-aware naming.

Each increment should remain separately deployable and removable. Workspace execution and external side effects need their own threat-model review before being enabled.
