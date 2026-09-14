# Demo instructions

These instructions run the Auth Portal locally with the repository's Datum-Sync v2 interface. Auth features are under **Authentication** in the left navigation.

## Start the demo

```bash
cd <repository root>
python demo/manage.py start
```

Open **http://127.0.0.1:8210** for the operator portal. Open **http://127.0.0.1:8220** for the federated agent worker.

- Username: **operator**
- Password: open `.runtime/operator.txt`. From Bash, run `cat .runtime/operator.txt`.

The private password file is excluded from Git. The demo keeps running after the terminal or browser closes.

## Stop, restart or inspect it

```bash
cd <repository root>
python demo/manage.py stop
python demo/manage.py restart
python demo/manage.py status
```

`stop` preserves all demonstration data and leaves the private PostgreSQL cluster running. After rebooting the machine, run `start` again.

## Demonstrate the federated Codex worker

1. Choose **Researcher** for the complete Office, PostgreSQL, map and WoRMS walkthrough. Use **GIS Analyst** for a spatial story, **Data Enginer** for a pipeline story, or another bundle from [PERSONAS.md](PERSONAS.md).
2. Open **http://127.0.0.1:8220** and choose **Connect agent**. Datum Sync opens its consent screen; select that persona agent and allow the connection.
3. Ask **List the Office documents I can access.** The right-hand trace shows Codex selecting a tool and Datum Sync executing it. Use the corners button in the pane header to expand the live trace into a large focused card; press Escape to restore the split view.
4. Ask **Summarize Auckland orders from PostgreSQL.** Open the worker’s **MCP activity** card to show its session-local tool trace. Open the operator portal’s **MCP Activity** only when you want to inspect the full deterministic authentication, authorization, dispatch and completion timeline.
5. In **Secrets & Proxies**, disable `postgres__orders_summary`, then repeat the prompt. The worker sees the updated catalogue or denial without restarting and has no direct database credential to bypass it.
6. Re-enable the tool, revoke the agent's PostgreSQL credential grant, and repeat. Restore the grant before continuing the demo.
7. Ask **Create a small HTML artifact that summarizes the Auckland results and save it for this agent.** Codex calls `resources_create`; the new private resource opens in the worker's **Artifacts** tab. Use the corners button to expand the preview. HTML runs inside a sandboxed frame; text, JSON, GeoJSON and CSV render as escaped text.
8. Ask **Share this artifact with designer.** Connect a second browser session as **Designer** to show the resource arriving as an explicit shared item. In the operator portal, open **Resources** to review its owner, version, type, active share, expiry and payload-free event history. Revoke the share there and show that Designer loses access on the next refresh.
9. Choose **Disconnect** to terminate the per-browser Codex process and discard its short-lived Datum Sync token.

The worker uses the locally installed Codex CLI 0.154.0 and `gpt-5.6-luna` by default. Run `codex login status` if model calls report that Codex is not signed in. Set `DATUM_WORKER_MODEL` before `demo/manage.py start` to demonstrate another supported Codex model.

## Demonstrate MCP fleet governance

1. Sign in and open **Secrets & Proxies**.
2. Under **Add MCP server**, add the **OfficeCLI document demo**, **PostgreSQL orders demo**, and **Datum Harness tools** if they are absent. The page discovers their tools and shows each server, service identity, health and state.
3. Open **Principals** to inspect the seven registered persona agents and their individual MCP access. Enrol another agent only when demonstrating custom registration.
4. To use the browser access lab directly, issue a baseline token for the chosen persona and choose **Open access lab**. Copy the token when shown; it is displayed only once and kept only in page memory.
5. The persona agents already have direct managed-credential grants matching their bundle. Use **MCP access** to review the exact servers and tools before opening the access lab.
6. Return to the access lab and run:
   - **Legacy:** list repositories, summarize jobs and list the agent's jobs.
   - **OfficeCLI:** list documents, read `quarterly-brief.docx`, and validate `platform-demo.pptx`.
   - **PostgreSQL:** list tables, sample Auckland orders, and summarize orders.
   - **Datum Harness:** list personas, render a Wellington map, and resolve two marine species through WoRMS.
7. Open **MCP Activity** and expand one of those calls. The timeline shows request receipt, authentication, session validation, tool authorization, upstream dispatch and completion. Arguments, returned document text and database rows are not copied into the log.
8. Open **Principals**, choose **MCP access** on the agent, and review its server policies, effective credential grants, live session and recent MCP flows.
9. Open **Secrets & Proxies** and disable `postgres__sample_orders`. Refresh or retry from the access lab: that tool disappears immediately while the other PostgreSQL tools remain available. Re-enable it.
10. Revoke one granted tool from the agent, then disable and re-enable the OfficeCLI server. The next catalogue/call reflects every change, and the connection-event list records the operator action.
11. Revoke the PostgreSQL credential grant. This removes that server's granted tools for the agent without revealing or changing the upstream credential.

The Datum Harness provider reads the real harness persona configuration, renders bounded Leaflet HTML without writing a file, and sends only requested names to the public WoRMS API. The OfficeCLI-shaped provider intentionally exposes one `officecli` tool but accepts only `list`, `view NAME text` and `validate NAME` for three sample documents. Create, edit, remove, batch and raw commands are denied. The PostgreSQL provider runs fixed, parameterized reads against the local `mcp_demo.orders` table and does not accept SQL.

## Demonstrate elevation and publication

1. From **Access lab**, read `demo/welcome.md`; a baseline write is denied.
2. Request elevation, approve it in **Approvals**, return to the lab and claim it.
3. Write the document with the elevated token. Reading `private/operator.md` remains denied.
4. Request publication and approve it. The release and audit record commit together.
5. Restrict and restore the agent. Its previous elevation remains revoked.

## Terminal MCP examples

Save an agent token that already has the relevant approved grants:

```bash
cd <repository root>
umask 077
read -rs -p 'Agent token: ' TOKEN; echo
printf '%s\n' "$TOKEN" > .runtime/my-agent.token
unset TOKEN

python demo/agent_demo.py office-list --token-file .runtime/my-agent.token
python demo/agent_demo.py office-read --document quarterly-brief.docx --token-file .runtime/my-agent.token
python demo/agent_demo.py office-validate --document platform-demo.pptx --token-file .runtime/my-agent.token
python demo/agent_demo.py postgres-tables --token-file .runtime/my-agent.token
python demo/agent_demo.py postgres-sample --region Auckland --limit 10 --token-file .runtime/my-agent.token
python demo/agent_demo.py postgres-summary --token-file .runtime/my-agent.token
python demo/agent_demo.py harness-personas --token-file .runtime/my-agent.token
python demo/agent_demo.py harness-map --token-file .runtime/my-agent.token
python demo/agent_demo.py harness-worms --token-file .runtime/my-agent.token
```

## If the page does not open

Run `demo/manage.py status`, then `demo/manage.py start`. Inspect `.runtime/portal.log`, `.runtime/worker.log`, `.runtime/legacy-mcp.log` and `.runtime/demo-mcp.log` if startup fails. Use the exact address **http://127.0.0.1:8210**.

See [README.md](README.md) for all commands and [INTEGRATIONS.md](INTEGRATIONS.md) for the implementation boundary.
