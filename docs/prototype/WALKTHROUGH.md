# Datum Sync demonstration walkthrough

This 10–12 minute walkthrough tells one story: an agent can be useful without possessing the credentials or authority behind its tools. The operator controls access in Datum Sync; a small Codex worker consumes that governed capability through MCP.

## Recording setup

Use a 1440p browser window at 100% zoom. Keep three tabs open:

1. **Datum Sync operator portal** — `http://127.0.0.1:8210`
2. **Datum federated worker** — `http://127.0.0.1:8220`
3. **Operator MCP Activity** — optional, for the later administration and audit detail

Start with the registered **Researcher** persona, which has OfficeCLI, bounded PostgreSQL analytics, maps and WoRMS access. Clear only disposable browser sessions if you need a clean take; keep the seeded providers and sample data. Avoid showing `.runtime/operator.txt`, bearer tokens, browser developer tools, or terminal environment variables in the recording.

## 1. Establish the problem — 45 seconds

Show **Principals** in the portal.

> “This is Datum Sync, an auth and governance plane for an agent fleet. The Researcher has its own identity and permission bundle here, but it does not own an Office credential, database password or upstream service token. It receives narrowly scoped access to tools.”

Open the demonstration agent’s **MCP access** view. Point out effective grants, active sessions, and recent connections.

## 2. Show the service boundary — 60 seconds

Open **Secrets & Proxies**. Show the OfficeCLI, PostgreSQL and Datum Harness server cards, discovered tools, managed service identity, health, and enable controls.

> “Operators register MCP servers and store upstream credentials once. Agents see only the tools allowed by their grants. The secret stays behind this proxy.”

Do not open or narrate implementation-only credentials. Emphasize tool names and state.

## 3. Connect the federated worker — 60 seconds

Switch to the worker. Point out the four status cards: agent, Luna model, Datum Sync MCP route, and disabled local tools. Choose **Connect agent**, select the demonstration agent on the Datum consent screen, and allow access.

> “The client is intentionally small. Codex owns the model and conversation loop. Datum Sync owns every operational capability. This consent produces a short-lived agent token for one worker session.”

After returning, show the green governed-session indicator, the **Researcher** display name, and its persona-specific starter prompts.

## 4. Run an OfficeCLI task — 90 seconds

Choose **Review the quarterly brief and test its claims against the available operations data.** Let the response finish. Keep the run trace visible.

> “Codex chose an MCP tool. The client shows only the server, tool, state, and duration. Arguments, document contents, and credentials are not copied into its operational trace.”

Open the worker’s **MCP activity** card and show the session-local call summary. Then, in the operator portal’s **MCP Activity**, expand the matching call and walk down request receipt, authentication, session validation, authorization, upstream dispatch, and completion.

## 5. Run a PostgreSQL task — 90 seconds

Ask **Summarize the available operations data and call out the regional differences.** Show the answer, then show its MCP trace.

> “The provider accepts a fixed read-only operation, not arbitrary SQL. Datum Sync verifies the agent, grant, server, and exact tool before dispatch. The database credential never enters the worker.”

## 6. Use real Datum Harness tools — 90 seconds

Ask **Resolve Crassostrea gigas and Perna canaliculus with WoRMS, compare their classifications, and use the persona catalogue to recommend who should continue the work.** Then ask **Create a Wellington Leaflet map and explain which evidence you would add next.**

> “These are real harness capabilities registered through the same governance plane. Persona discovery reads the harness source configuration. Taxonomy lookup makes a bounded, read-only request to the public WoRMS service; only the requested scientific names leave the machine.”

Optionally ask for a Wellington Leaflet map to show a local computation tool that needs no upstream credential or file write.

## 7. Demonstrate live revocation — 2 minutes

In **Secrets & Proxies**, disable `postgres__orders_summary`. Return to the worker and repeat the same request.

> “Revocation applies at the next catalogue or call. There is no worker restart and no credential to rotate inside the agent.”

Show the denial or absence of the tool, then show the corresponding Datum Sync activity. Re-enable the tool. Next revoke the agent’s PostgreSQL credential grant and repeat once more. Explain that server state, tool state, and per-agent credential grants are separate controls. Restore the grant after the take.

## 8. Show fleet review — 75 seconds

Return to **Principals → MCP access** for the agent.

> “This is the operator’s answer to three questions: which servers can this agent reach, which managed credential grants make that access effective, and what has it called recently?”

Mention that the current prototype is single-operator but all core records already attach to explicit principals and grants, which is the seam for tenant and user ownership.

## 9. Close the session — 30 seconds

Choose **Disconnect** in the worker.

> “Disconnecting terminates this browser session’s Codex worker and discards its short-lived Datum Sync token. Upstream Office and database credentials remain in Datum Sync.”

End on **MCP Activity** with the completed and denied calls visible.

## Claims to keep precise

- OfficeCLI is an OfficeCLI-shaped compatibility provider with constrained sample documents; it is not the upstream OfficeCLI package.
- PostgreSQL is a real local database exposed through fixed parameterized read operations; arbitrary SQL is not accepted.
- The curated harness provider loads real persona configuration, adapts Leaflet rendering, and calls the public WoRMS API. Pipeline execution, vault access, dynamic code creation, CUA, and desktop control remain excluded.
- Codex app-server is experimental in the pinned CLI build, so this is a proof of concept client boundary.
- The worker configuration disables Codex local execution and non-Datum tool surfaces. Production isolation still requires a dedicated container or OS identity plus network egress policy.
- The current portal demonstrates one operator and a fleet of agents. Multi-user tenancy needs tenant ownership, operator roles, and isolation applied to the same principal, grant, session, and audit records.
