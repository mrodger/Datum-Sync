# Datum persona agents

The local prototype registers the seven personas from demo/personas.py as individual Datum Sync agent principals. Each principal is owned by the operator, selectable in the federated worker, independently revocable, and attached to an explicit MCP policy plus managed-credential grants.

| Agent | Available MCP capability | Resource authority |
|---|---|---|
| Manager | Legacy metadata and jobs, Office documents, PostgreSQL reads, persona discovery | Read, elevated write, elevated publish |
| Developer | Legacy metadata and jobs, PostgreSQL reads, persona discovery | Read and elevated write |
| Researcher | Office documents, bounded PostgreSQL analytics, personas, maps and WoRMS | Read only |
| Designer | Office documents, personas and map rendering | Read, elevated write, elevated publish |
| Security | Legacy metadata and jobs, PostgreSQL schema inspection, persona discovery | Read only |
| GIS Analyst | Legacy metadata and jobs, PostgreSQL reads, personas, maps and WoRMS | Read, elevated write, elevated publish |
| Data Enginer | Legacy metadata and jobs, Office documents, PostgreSQL reads, personas and maps | Read, elevated write, elevated publish |

All agents deny private/**, have one concurrent MCP session, and use tier 2 for normal tool calls with a tier 3 ceiling for human-approved operations. The upstream service credentials remain in Datum Sync and can be revoked per agent, per tool, or per server.

## Demo prompts

Each worker shows three prompts chosen to exercise only the tools in that agent's governed bundle. They are written as useful tasks rather than tool commands, so the demonstration shows the model selecting MCP tools through Datum Sync.

### Manager

1. Give me an executive status summary of current jobs and outstanding work.
2. Compare regional order activity and flag anything that needs attention.
3. Review the available Office documents and prepare a concise project briefing.

### Developer

1. Inspect the available repositories and workspaces, then summarize their current job status.
2. Describe the demo database schema and show a safe sample of recent orders.
3. Investigate any failed jobs using status, events and logs, then suggest next steps.

### Researcher

1. Resolve Crassostrea gigas and Perna canaliculus with WoRMS, compare their classifications, and use the persona catalogue to recommend who should continue the work.
2. Review the quarterly brief and test its claims against the available operations data.
3. Create a Wellington Leaflet map and explain which evidence you would add next.

### Designer

1. Review the platform demo deck and propose a clearer six-slide narrative.
2. Create a Wellington map with labelled points for a product walkthrough.
3. Review the available personas and propose a simple visual identity for each.

### Security

1. Audit the exposed MCP surface and summarize which systems, tools and schemas are visible.
2. Review recent job events and logs for failures without exposing payloads.
3. Explain how revoking one database tool changes this agent's effective access.

### GIS Analyst

1. Create a Wellington map and add labelled points for field operations.
2. Resolve two New Zealand marine species with WoRMS and summarize their environments.
3. Combine regional order statistics with current job status into a spatial operations brief.

### Data Enginer

1. Inspect the orders schema and propose a governed transformation pipeline.
2. Summarize job health and identify failed or incomplete processing stages.
3. Validate the fleet workbook and create a Wellington map for the output.

## Prompt completeness

The harness includes a name, short role, icon, colour, pane preference and three starter prompts for each persona. It expects richer Markdown definitions under ~/vault/personas/, but that directory is absent on this machine.

Each registered agent is therefore marked definition_complete: false and uses the harness fallback form:

    You are Datum's <Display Name> AI. Your focus: <role>.
    Be terse and direct. No filler. Answer first, explain if needed.
    No emojis unless asked. Use markdown formatting where appropriate.

The federated worker appends that text to its fixed Datum governance instructions when the operator selects the persona. Adding the missing detailed definitions later should create a new bundle version and an explicit review rather than silently changing existing agents.

## Provisioning

The local provisioning command is deliberately explicit:

    python demo/provision_personas.py --register

To remove browser-test data, restore seeded documents, clear test activity and register the persona fleet in one transaction:

    python demo/provision_personas.py --clean-test-artifacts --register

The cleanup preserves the operator, MCP server registry, encrypted upstream credentials, provider catalogue and PostgreSQL demonstration data. It removes browser-agent principals, their jobs and authorization records, test activity timelines, dynamic OAuth clients and generated browser artifacts. It also invalidates existing portal and worker sessions, so sign in again after running it.
