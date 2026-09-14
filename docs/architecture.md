# Datum Sync and the federated worker

Datum Sync is a deterministic authority plane for a fleet of AI agents. It contains
no model. The worker is a small model client that contains no credentials. The
whole design is the line between them, and this document is about that line.

```
┌──────────────────────────────┐   OAuth consent, then one     ┌──────────────────────────────┐
│  FEDERATED WORKER   :8220    │   short-lived agent token     │  DATUM SYNC   :8210 (portal) │
│                              │ ───────────────────────────▶ │               :8200 (API)    │
│  Codex conversation loop     │                               │  principals · grants         │
│  chat UI, run trace,         │   JSON-RPC over /mcp          │  sessions · MCP registry     │
│  session-local cards         │ ───────────────────────────▶ │  sealed upstream credentials │
│                              │                               │  resources · audit · flows   │
│  holds: DATUM_SYNC_TOKEN     │ ◀─────────────────────────── │                              │
│  (opaque relay credential)   │   results, catalogue, denials │  decides · injects · records │
└──────────────────────────────┘                               └───────────┬──────────────────┘
                                                                           │ pinned egress, secret
                                                                           │ injected server-side
                                                     ┌─────────────────────┼─────────────────────┐
                                                     ▼                     ▼                     ▼
                                              legacy adapter :8211   OfficeCLI demo :8212   PostgreSQL / harness :8212
                                              (read-only registry,   (list · view · validate) (fixed reads · personas ·
                                               jobs, events)                                   Leaflet · WoRMS)
```

## Who owns what

| | Datum Sync | Federated worker |
|---|---|---|
| **Owns** | Principals, grants, sessions, the MCP server registry, managed upstream credentials, governed resources, the audit trail and MCP flow evidence | The conversation loop, the model (Codex, `gpt-5.6-luna` by default), the chat UI, a session-local view of its own calls |
| **Holds** | Sealed upstream secrets (AES-256-GCM, key-id prefixed so keys rotate), the operator identity, the policy | One opaque agent token bound to the consented session, rotated behind a worker-local relay credential |
| **Decides** | Which agent, which server, which tool, which credential, at which tier, for how long — re-checked on every call | Nothing about access. It asks, Datum Sync answers |
| **Never sees** | Prompts, model reasoning, tool arguments, returned document text, database rows | Secret values, other agents' resources, the fleet inventory, the operator's audit trail |
| **Replaceable?** | No — it is the system of record | Yes — any MCP-capable client can stand in its place |

The worker's Codex profile disables the shell, unified exec, web search, browser,
computer use, image generation, apps and plugins (`worker/codex_bridge.py`). With an
empty per-session working directory, the Datum Sync MCP endpoint is the only
operational route the model can take.

## The request path

1. **Consent.** The worker redirects to the portal's `/oauth/authorize`. The operator picks an agent and a session length — 15 minutes for a demo, or 2, 4 or 8 hours — and approves. Datum Sync enforces that deadline absolutely; refresh cannot extend it (`migrations/023`, `portal.py`).
2. **Token.** The worker receives a short-lived access token and a refresh token for that agent. It hands the Codex process only `DATUM_SYNC_TOKEN`, an opaque relay credential, and rotates the real token behind it without breaking the conversation.
3. **Call.** Every tool call is a JSON-RPC request to `/mcp` carrying the agent token. Nothing else.
4. **Policy.** Datum Sync resolves the principal, checks its state, validates the session, then checks — in order — that the server is active, the tool is enabled, the agent's grant names that tool, the tool's minimum tier is met, and a managed-credential grant exists for the upstream connection.
5. **Dispatch.** For a federated tool the upstream secret is unsealed server-side and injected into the request (`proxy.inject_auth`). Outbound traffic goes through `egress.PinnedTransport`: DNS resolved once, private and loopback addresses refused for external targets, redirects refused, response size capped. Demo providers are further restricted to registered loopback port/path pairs (`federation._local_url`).
6. **Evidence.** The whole flow is recorded as a deterministic, payload-free timeline in `mcp_flows` / `mcp_flow_events`: `rpc.parsed → auth.accepted → session.validated → tool.authorized → upstream.started → upstream.completed → tool.completed`, or `tool.denied` / `session.denied`. Identity is copied into the record so it survives principal or credential deletion. Arguments, prompts, secrets and results are never stored.

The built-in tools the portal itself serves (`whoami`, `read`, `documents_read`,
`documents_write`, `publish`, `resources_*`) follow the same path minus dispatch.

## Four independent revocation controls

| Control | Where | Effect |
|---|---|---|
| Tool | Secrets & Proxies → Disable tool | The tool leaves the catalogue; a call to it is `tool.denied` on the next request |
| Server | Secrets & Proxies → Disable server | Every tool on that provider is unavailable |
| Grant | Secrets & Proxies → grants → Revoke server access | This agent loses the upstream credential for that server; other agents keep theirs |
| Agent | Principals → Disable (or Restrict) | Every credential is revoked and every session closed; the worker sees a denial, not a crash |

None of these restart anything or rotate anything inside the worker, because the
worker was never given anything to rotate.

## Governed resources

Agents save output with `resources_create`. Each resource is an immutable version
with an owner, logical path, content hash, MIME type, source trace and expiry
(`migrations/022`). Resources start private. `resources_share` grants time-bounded
read access to one named agent in the same fleet; `resources_revoke`, or the
operator's Resources page, removes it. The worker renders HTML resources in a
sandboxed iframe with no same-origin access and no network; other formats render as
escaped text. Resource content is excluded from the flow and audit records.

## What a production agent gateway must get right

These are the questions the prototype was built to answer, with an honest statement
of what is done and what remains.

**Identity.** Every agent is a first-class principal — owner, state, grant, audit
trail — not a shared API key. *Done.* Multi-tenancy needs tenant ownership and
operator roles applied to the same records; the parent-owner boundary is the seam.

**Least privilege.** Grants name exact tools, not servers. Tiers cap what any token
can ever do. Sessions end on an absolute deadline the operator chose. *Done.* In
production, policy should be reviewed configuration, not portal clicks.

**Credential brokering.** Upstream secrets are write-only, versioned, rotated only
after a live `initialize` + `tools/list` probe succeeds, and injected server-side.
The worker, the model and the browser never see a value. *Done.* Keys should move to
an HSM or KMS with per-tenant key sets.

**Egress control.** Pinned-IP transport, private ranges refused, redirects refused,
response size capped, demo providers pinned to loopback. *Done in-process.* Network
policy must also be enforced outside the process — a container or OS identity per
worker, with an egress allow-list that does not depend on model configuration.

**Evidence.** A deterministic, payload-free timeline for every call, with denials in
the same stream as successes. *Done.* Production adds export to a SIEM, retention and
legal hold.

**Isolation.** The worker holds no secrets and its local tools are off; artifacts run
sandboxed. *Done at the application layer.* `danger-full-access` appears in the Codex
thread configuration only because the demo host cannot create the user namespace
bubblewrap needs; a production worker runs in its own container or OS account.

**Honesty about the demo providers.** The OfficeCLI provider is OfficeCLI-shaped and
accepts only `list`, `view NAME text` and `validate NAME` for three sample documents.
The PostgreSQL provider runs fixed parameterised reads against `mcp_demo.orders`; it
does not accept SQL. The harness provider loads real persona configuration, renders
bounded Leaflet HTML and sends only the requested names to the public WoRMS API. The
Codex app-server protocol is experimental in the pinned CLI build.

## Where things live

| Concern | Code | Schema | Tests |
|---|---|---|---|
| Portal app, consent, sessions, built-in tools | `datum_sync/portal.py`, `portal_core.py` | `016`, `023` | `tests/test_portal.py` |
| MCP federation and tool catalogue | `datum_sync/federation.py` | `017`, `021` | `test_mcp_federation*.py`, `test_mcp_server_registry.py` |
| Managed credentials, requests, grants | `datum_sync/credential_access.py` | `020` | `test_credential_access.py` |
| Flow evidence | `datum_sync/mcp_observe.py` | `019` | `test_mcp_observability.py` |
| Governed resources | `datum_sync/resources.py` | `022` | `test_governed_resources.py` |
| Pinned egress | `datum_sync/egress.py`, `proxy.py` | — | `test_security_repairs.py` |
| Job ownership and events | `datum_sync/jobs.py` | `018` | `test_jobs.py` |
| Demo providers | `datum_sync/legacy_mcp.py`, `demo_mcp.py` | `021` (sample data) | `test_mcp_server_registry.py` |
| Persona bundles | `datum_sync/persona_profiles.py`, `demo/personas.py` | — | `test_persona_profiles.py` |
| Worker | `worker/app.py`, `codex_bridge.py`, `datum_sync_client.py` | — | `worker/tests` |
| Local stack | `demo/manage.py`, `demo/provision_personas.py` | — | `demo/manage.py test` |
