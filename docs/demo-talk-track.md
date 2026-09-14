# Demo talk track

Ten clips, about twelve minutes live or eight minutes of video. Each row gives the
clip from `docs/walkthrough/video/`, what is on screen, the sentence to say, and the
production-gateway point that scene earns you. Details and the honest gap list are in
[architecture.md](architecture.md).

| # | Clip | On screen | Say | The production point |
|---|---|---|---|---|
| 1 | `01-problem` | Portal → Principals → Researcher → MCP access | "This is an auth and governance plane for a fleet of agents. The Researcher has an identity and a permission bundle. It does not own an Office credential, a database password or an upstream token." | **Identity.** An agent is a principal with an owner, a state and an audit trail — not a shared API key pasted into a prompt. |
| 2 | `02-boundary` | Secrets & Proxies: servers, tools, managed identities | "Operators register MCP servers and store upstream credentials once. Agents see only the tools their grants allow. The secret stays behind this proxy." | **Least privilege.** Grants name exact tools. Each tool carries a minimum tier. Nothing is inherited from "the server". |
| 3 | `03-connect` | Worker → Connect → consent (agent + session length) → connected | "The client is small on purpose. Codex owns the model loop; Datum Sync owns every operational capability. This consent produces one short-lived token for one session." | **Bounded sessions.** The operator chose the deadline; refresh cannot extend it. When it ends, the worker simply stops being able to ask. |
| 4 | `04-office-task` | Prompt → run trace → worker MCP card → portal timeline | "Codex chose an MCP tool. The worker's trace shows server, tool, state and duration. The operator's timeline shows receipt, authentication, session, authorization, dispatch, completion — and never the arguments, the document or the result." | **Evidence without payloads.** You can prove what happened without keeping what was said. That is what makes the trail safe to retain and export. |
| 5 | `05-postgres-task` | Prompt → answer → worker "proxies" card | "The provider accepts a fixed read-only operation, not SQL. Datum Sync verified the agent, the grant, the server and the exact tool before dispatch. The database credential never entered the worker." | **Credential brokering.** Secrets are write-only, versioned, rotated after a live probe, injected server-side. The model, the browser and the worker process only ever see a label and a fingerprint. |
| 6 | `06-harness-tools` | WoRMS + Leaflet prompts, run trace | "These are real harness capabilities behind the same plane: persona discovery reads real configuration, the taxonomy lookup is a bounded read-only call to WoRMS, the map is rendered locally with no credential and no file write." | **Egress control.** Outbound calls go through a pinned-IP transport that refuses private ranges and redirects and caps the response. Demo providers are further pinned to loopback. |
| 7 | `07-revocation` | Disable `postgres__orders_summary` → repeat prompt → denial in MCP Activity → re-enable | "Revocation applies at the next call. No worker restart, no credential to rotate inside the agent — it was never given one. The denial lands in the same evidence stream as the successes." | **Revoke immediately, separately.** Tool, server, grant and agent are four independent controls. In production these become reviewed configuration, not portal clicks. |
| 8 | `08-fleet-review` | Principals → MCP access; Resources | "This answers the operator's three questions: which servers can this agent reach, which grants make that effective, and what has it called recently." | **Tenancy is a seam, not a rewrite.** Every record already attaches to an explicit principal and grant; tenant ownership and operator roles apply to the same rows. |
| 9 | `09-artifacts` | Create HTML artifact → split/focused pane → share with Designer → operator Resources | "Output is saved through Datum Sync as a versioned, private resource. A share is a named, time-bounded read grant. The operator can see it and revoke it." | **Governed outputs.** Artifacts are not files passed through chat. Content stays out of the audit trail; ownership and shares stay in it. HTML renders sandboxed. |
| 10 | `10-close` | Disconnect → MCP Activity | "Disconnecting terminates this browser's Codex process and discards its short-lived token. Upstream credentials remain in Datum Sync." | **Isolation.** The worker holds nothing worth stealing, so replacing it — or losing it — costs nothing. Production adds a container or OS identity per worker and network policy outside the process. |

## The one-line close

> Connect to a useful agent while identity, MCP access, credentials, evidence and
> artifacts remain governed in one place.

## Claims to keep precise

- The OfficeCLI provider is OfficeCLI-shaped with three sample documents; it is not the upstream package.
- PostgreSQL is a real local database behind fixed parameterised reads; arbitrary SQL is not accepted.
- The harness provider loads real persona configuration, renders Leaflet locally and sends only requested names to the public WoRMS API.
- The Codex app-server protocol is experimental in the pinned CLI; the worker is a proof of the client boundary.
- The portal is single-operator today. Multi-user tenancy applies tenant ownership, operator roles and isolation to the existing principal, grant, session and audit records.

## If something goes wrong live

- Worker will not answer: `codex login status`, then `python demo/manage.py restart`.
- A tool is missing from the catalogue: check Secrets & Proxies for a disabled tool or server left over from scene 7.
- Reset the fleet to a clean state: `python demo/provision_personas.py --clean-test-artifacts --register`.
- Play the clip instead: every scene exists as a standalone video.
