# MCP fleet governance requirement review

This review measures the local prototype against the requested minimum: register agents, proxy MCP and credential access, review access per agent, observe connections, add and revoke servers/tools, govern one operator's agent fleet, and show a credible path to multiple users.

| Requirement | Current result | Evidence and boundary |
|---|---|---|
| Register an agent | Met | One-use enrolment, human approval, baseline token issuance and lifecycle controls are exposed in the v2 UI. |
| Proxy agent MCP access | Met for registered HTTP providers | The gateway owns MCP sessions, filters `tools/list`, authorizes every call, constructs trusted principal context and forwards only to three exact loopback routes. |
| Proxy agent credential access | Met | Upstream service tokens are encrypted and injected after live principal, ancestry, tier, policy, credential and exact-tool grant checks. Agents receive use authority and never receive the secret. |
| Review MCP and credential status per agent | Met for the single-operator fleet | **Principals → MCP access** shows applicable server policies, effective tools, credential state, grant expiry, sessions and recent flows for one agent. |
| Log MCP/auth connections | Met | Payload-free MCP flow timelines cover authentication through provider outcome. Separate server and credential lifecycle events record registration, refresh, approval, disablement and revocation. |
| Revoke a server or tool | Met | Operators can disable a server or tool for the fleet, revoke one tool from one agent's grant, revoke the whole grant, or disable the upstream credential. Every action affects the next catalogue/call. |
| Add a server or tools | Met for vetted demo templates | Operators can add OfficeCLI and PostgreSQL demo servers. Tools are discovered from MCP and stored. Arbitrary URL/stdio onboarding and manually invented tools are intentionally absent. |
| Govern one user's agent fleet | Met | MCP connections are owned by the operator; the server, credential, request, approval, grant and event paths enforce that owner relationship. |
| Expand to multiple users | Foundation present; not complete | `connections.owner_id` and scoped MCP/credential endpoints establish fleet ownership. Principal, OAuth, session, resource, flow and general audit tables/queries still lack a tenant boundary. Server names are globally unique and there is one operator role. |

## Demonstration providers

The Office provider mirrors the upstream OfficeCLI MCP shape by exposing one `officecli` command tool. Its local allowlist supports document listing, text viewing and validation for three sample files. It rejects write, delete, batch and raw command forms. The actual OfficeCLI package is not installed, so this proves the governance and compatibility path rather than Office file processing.

The PostgreSQL provider queries a real local `mcp_demo.orders` table. It exposes structured schema/table discovery, table description, bounded samples and summaries. It never accepts SQL. The operations are fixed and parameterized, although the local process shares the application database role. A production provider should run under a separate database role with only the intended read privileges.

Both providers require distinct service tokens, accept server-generated principal metadata only after gateway authorization, and are reached through the same credential grant and flow logging path as the legacy adapter.

## Remaining work before broader deployment

1. Add tenant IDs and tenant-scoped keys/queries for principals, credentials, sessions, OAuth records, resource data, MCP flows and audit data; define operator roles and cross-tenant administration.
2. Add a supervised stdio-to-HTTP bridge for upstream packages such as OfficeCLI and Microsoft PostgreSQL MCP. Pin executable and configuration choices, isolate processes, bound traffic and manage restarts.
3. Add provider onboarding validation for non-loopback destinations, TLS identity, redirect rules, supported authentication profiles and secret tests before allowing arbitrary MCP URLs.
4. Give each database provider a least-privilege role and place service secrets in an external key manager with recovery and rotation operations.
5. Add distributed rate controls, retention/export policy, backup/restore, service supervision, public TLS deployment and an independent security review.

The requested single-user governance loop is demonstrable now. The UI makes the multi-user expansion seam visible through operator-owned servers, but the prototype should not yet be described as tenant-isolated.
