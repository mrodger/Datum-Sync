# Datum Auth Portal — agent auth plane prototype

This branch (`prototype/agent-auth-plane`) ports a standalone prototype onto the Datum Sync service. The portal lives in `datum_sync/portal.py` and its helpers, its schema in `migrations/016`–`023`, the federated worker in `worker/`, the local launcher in `demo/`, the five-page product overview in `docs/walkthrough/` and the security reviews in `docs/review/`. The documents in this directory were written for the standalone prototype and have had their paths updated; where they describe "this machine" they mean the Ubuntu host the prototype was built and verified on.

For start, stop, sign-in and a guided demo, see [INSTRUCTIONS.md](INSTRUCTIONS.md). For the narrated demonstration sequence, see [WALKTHROUGH.md](WALKTHROUGH.md). The registered agent bundles are documented in [PERSONAS.md](PERSONAS.md).

A working auth portal built from the v1 code and reviewed v2 proposal. It runs at **http://127.0.0.1:8210**. Sign in as **operator** using the generated password in `.runtime/operator.txt`. That file is private and excluded from Git.

The demo uses the supplied **Datum-Sync v2 UI** from `datum_sync/static-v2/`: the original HTML shell, stylesheet, Datum mark, icons and collapsible navigation. Auth screens use its dashboard, table and form layouts. The dedicated portal entry point supplies auth data and actions.

The portal supports agent enrolment, human approval, baseline credentials, bounded OAuth elevation, operator-owned MCP server registration, discovered tool controls, write-only managed upstream credentials, reviewable credential requests, MCP sessions, per-agent and fleet-wide revocation, publication approvals and deterministic MCP request traces. The access lab makes real gateway calls to the legacy v1 registry/job adapter, an OfficeCLI-shaped document provider, a read-only PostgreSQL provider backed by the local database, and a curated provider built from Datum Harness tools.

## Try the workflow

1. Sign in and open **Secrets & Proxies**. Register the **OfficeCLI document demo**, **PostgreSQL orders demo**, and **Datum Harness tools** if they are not already present. Their tool catalogues are discovered through MCP and become separately controllable.
2. Connect the federated worker to one of the seven registered persona agents. Use **Enrol agent** when demonstrating creation of an additional custom agent.
3. Review the persona’s pre-provisioned managed-credential grants under **Principals → MCP access**. The agent receives exact tool-use grants and never receives an upstream service token.
4. Run the visible examples: list legacy repositories and jobs; list, read and validate the demo Office documents; list PostgreSQL tables, sample Auckland orders and summarize orders; then list harness personas, render a Wellington map, and resolve marine species with WoRMS.
5. Open **MCP Activity** and expand a call. Its deterministic timeline shows authentication, session validation, authorization, upstream dispatch and completion without storing arguments or result payloads.
6. Open **Principals → MCP access** to review one agent's servers, effective credential grants, live sessions and recent MCP flows.
7. In **Secrets & Proxies**, disable a tool or its whole server and retry the call. Re-enable it, then revoke one tool from the agent's credential grant. Each change applies to the next catalogue or call and appears in the connection-event log.
8. The original auth workflow remains available: read `demo/welcome.md`, observe a baseline write denial, request and approve elevation, write the document, then request and approve publication.

The cleaned live prototype contains the seven persona agents rather than browser-test principals. Tokens entered in the access lab are held in browser memory, not local storage.

At connection, users choose a 15-minute demo session or a 2, 4, or 8-hour working session; Datum Sync enforces the absolute deadline while the worker rotates short-lived access credentials. The federated worker keeps normal use in one place: its Agent session, MCP activity, Secrets & proxies and Principal cards show the selected agent’s session-local, read-only view. The Datum Sync portal remains the operator surface for fleet changes, credential grants and global audit history.

Agent artifacts now use governed Datum Sync resources. They are versioned and private by default, can be shared with a named agent in the same fleet, appear in the worker's sandboxed Artifacts pane, and can be reviewed or revoked from the portal's Resources page. Content is excluded from deterministic flow and audit records.

## Product overview

The five-page landscape product brochure is available as [Word](../walkthrough/output/Datum-Sync-Walkthrough.docx) and [PDF](../walkthrough/output/Datum-Sync-Walkthrough.pdf). It uses the Datum Harness visual language, large annotated live screenshots and a compact ten-minute demonstration path across agents, MCP, credentials, evidence and governed resources. The OfficeCLI and Playwright build sources are in [docs/walkthrough](../walkthrough/README.md).

## Run

Register or refresh the persona fleet with `python demo/provision_personas.py --register`. See [PERSONAS.md](PERSONAS.md) for the permission matrix and prompt limitations.


From the repository root:

```bash
python demo/manage.py status
python demo/manage.py start
python demo/manage.py restart
python demo/manage.py stop
```

The auth portal runs at **http://127.0.0.1:8210** and the Datum federated worker runs at **http://127.0.0.1:8220**. The worker uses the locally installed Codex CLI with `gpt-5.6-luna`; it requires an existing Codex sign-in. Its only business-tool connection is the Datum Sync MCP gateway.

The services run independently of the terminal. `start` also starts its private PostgreSQL cluster when necessary. `stop` stops the portal, federated worker, legacy adapter and shared MCP provider process, and leaves the database running. Startup after a machine reboot is manual; no system service was installed. Logs, configuration, database files and the generated password are under `.runtime/` (excluded from Git). The portal listens on loopback port **8210**. Its private legacy MCP adapter listens on **8211**. The Office, PostgreSQL and curated Datum Harness MCP providers share a private process on **8212**. Their MCP routes require independent service credentials and are intended to be reached through the auth gateway. PostgreSQL uses loopback port **55435** and a private Unix socket. The original v1 workspace server and full coding harness are not started. The stripped `worker/` client replaces their agent loop for this demonstration. Its shell reuses the original harness design system, Datum mark, fonts and core chat components while retaining the smaller governed-worker behavior.

The launcher expects a `.venv` at the repository root (falling back to the running interpreter) and PostgreSQL 16, PostGIS and Git under `~/.local/share/datum-tools/`. [demo/requirements.lock.txt](../../demo/requirements.lock.txt) records the exact Python environment and [demo/system-packages.txt](../../demo/system-packages.txt) the private Ubuntu packages, with versions and checksums. Moving to another machine requires provisioning those prerequisites and adjusting `demo/manage.py` if their location differs. Chromium is needed for the Playwright checks.

## Verify

```bash
python demo/manage.py test       # portal + security regressions, isolated DB
python demo/verify.py suite      # complete Python regression suite
python demo/verify.py guards     # affected security mutation checks
python demo/check_schema.py      # compare both DBs to a fresh migration build
python demo/browser_smoke.py     # visible end-to-end auth flow
python demo/source_ui_check.py   # source assets, v2 geometry and mobile navigation
```

Run these sequentially: mutation checks temporarily edit and restore source. Database tests use only `datum_portal_test`, which they clear. The browser check exercises the running prototype and leaves a labelled demonstration agent, document change, release, audit history and payload-free MCP flow timeline. Screenshots are saved under `review/screenshots/`. The application database role cannot create databases; the normal suite therefore skips its schema-creation test. `check_schema.py` independently checks both schemas using a temporary database created through the private administrative socket, without promoting the application role.

## Connect a client

MCP endpoint: `http://127.0.0.1:8210/mcp`. Use an agent bearer credential and preserve the `Mcp-Session-Id` returned by initialization. Close the session when finished. The local client does this automatically:

```bash
# Save a token issued in the portal to a private file; do not put it in Git.
umask 077
read -rs -p 'Agent token: ' TOKEN; echo
printf '%s\n' "$TOKEN" > .runtime/my-agent.token
unset TOKEN
python demo/agent_demo.py whoami --token-file .runtime/my-agent.token
python demo/agent_demo.py read --token-file .runtime/my-agent.token
python demo/agent_demo.py legacy-repositories --token-file .runtime/my-agent.token
python demo/agent_demo.py legacy-workspaces --repository Testing --token-file .runtime/my-agent.token
python demo/agent_demo.py job-summary --token-file .runtime/my-agent.token
python demo/agent_demo.py jobs --token-file .runtime/my-agent.token
# Copy a visible job id from that result.
python demo/agent_demo.py job-events --job-id UUID --token-file .runtime/my-agent.token
python demo/agent_demo.py job-artifacts --job-id UUID --token-file .runtime/my-agent.token
python demo/agent_demo.py office-list --token-file .runtime/my-agent.token
python demo/agent_demo.py office-read --document quarterly-brief.docx --token-file .runtime/my-agent.token
python demo/agent_demo.py postgres-tables --token-file .runtime/my-agent.token
python demo/agent_demo.py postgres-sample --region Auckland --limit 10 --token-file .runtime/my-agent.token
python demo/agent_demo.py postgres-summary --token-file .runtime/my-agent.token
python demo/agent_demo.py harness-personas --token-file .runtime/my-agent.token
python demo/agent_demo.py harness-map --token-file .runtime/my-agent.token
python demo/agent_demo.py harness-worms --token-file .runtime/my-agent.token
python demo/agent_demo.py elevate --token-file .runtime/my-agent.token --save-token .runtime/elevated.token
# Approve the displayed code in the portal while the client waits.
python demo/agent_demo.py write --token-file .runtime/elevated.token --content 'An approved agent wrote this.'
```

The CLI commands require the matching credential grant approved in the portal. It also supports enrol/claim, publication, `legacy-manifest --repository NAME --workspace NAME`, and `job-status`, `job-log`, `job-events` and `job-artifacts` with `--job-id UUID`. Event and log reads accept `--after-id` and `--limit`. Artifact results contain metadata only. Run `--help` for arguments. HTTP discovery, device authorization and S256 PKCE are implemented for the prototype; broad third-party client certification is not claimed. The full harness application is not launched. The curated provider reads its persona configuration and adapts its map and WoRMS utilities behind Datum Sync. Its HTTP bridge now requires **per-server** `bearer_token_env_var` configuration; the old global bearer fallback is intentionally removed. `DATUM_MCP_SERVERS` applies to every configuration source. Harness authentication defaults on and requires a strong session secret and password hash.

## Scope and review

See [INTEGRATIONS.md](INTEGRATIONS.md) for the incremental service plan, plus [IMPLEMENTATION.md](IMPLEMENTATION.md), [docs/review/REVIEW.md](../review/REVIEW.md), [docs/review/CREDENTIAL_ACCESS_REVIEW.md](../review/CREDENTIAL_ACCESS_REVIEW.md), [docs/review/MCP_FLEET_GOVERNANCE_REVIEW.md](../review/MCP_FLEET_GOVERNANCE_REVIEW.md) and [docs/review/DELIVERY.md](../review/DELIVERY.md). The original v2 specification remains unchanged as historical input.

This is a local proof of concept, not a production deployment or the full v2 platform. The Codex app-server interface is experimental and the demo currently expects Codex CLI 0.154.0. The resource provider is a controlled database fixture; it does not execute agent code, shell commands or external side effects. The Office route is a constrained compatibility demonstration over in-memory example documents; the upstream OfficeCLI package is not installed. PostgreSQL queries a real local `mcp_demo.orders` table through fixed read-only operations, although the demo process still uses the application database role. The harness provider performs local persona discovery and map generation; `harness__worms_lookup` sends only the requested scientific names to the public WoRMS API. Production work still includes sandboxed execution, a supervised stdio bridge for packages such as OfficeCLI and Microsoft PostgreSQL MCP, arbitrary provider onboarding, full multi-operator tenancy, external key management and credential recovery, distributed rate limits, retention/backup policy, TLS hosting and independent security review.
