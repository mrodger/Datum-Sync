# Datum Federated Worker

This is the stripped demonstration client for Datum Sync. It keeps Codex's conversation and model loop, uses `gpt-5.6-luna` by default, and presents a compact Datum-branded interface. All business tools are supplied by one required MCP connection to Datum Sync.

The worker stores no Office or PostgreSQL credentials. At consent, the user chooses a 15-minute demo session or a 2, 4, or 8-hour working session. Datum Sync enforces that absolute deadline and issues rotating short-lived agent access tokens. The Codex app-server receives only an opaque worker-local relay credential as `DATUM_SYNC_TOKEN`; the worker rotates its Datum credential behind that relay without breaking the conversation or MCP session. Datum Sync exchanges managed upstream credentials when an allowed tool runs. All session credentials and the Codex process are discarded on disconnect or expiry.

The Codex profile disables shell, unified execution, web search, image, browser, computer-use, plugin, app, skill-search, and multi-agent tools. `danger-full-access` appears in the internal Codex thread configuration solely because this host cannot create the user namespace required by bubblewrap; with execution tools disabled and an empty per-session working directory, Datum Sync is the only model-visible operational route. A production worker should also run in a container or dedicated OS account and enforce outbound network policy independently of model configuration.

The installed Codex app-server protocol is experimental. The demo pins its expected CLI version in the root instructions and should be retested when Codex is upgraded.

## Running

```
pip install -r worker/requirements.txt
python -m uvicorn app:app --app-dir worker --host 127.0.0.1 --port 8220
pytest worker/tests
```

`DATUM_SYNC_URL` (default `http://127.0.0.1:8210`), `WORKER_URL` (default `http://127.0.0.1:8220`), `DATUM_WORKER_MODEL` and `CODEX_BIN` may be set in the environment or in `worker/.env`. `demo/manage.py start` launches the worker together with the portal and demo providers.

## Harness UI reuse

The worker reuses the Datum Harness design system. `static/branding/` holds the vendored copies it serves through curated read-only asset routes: `harness.css` (the harness `style.css`), the Datum mark, and the DM Sans, Space Grotesk and JetBrains Mono font files. Its HTML follows the harness sidebar, header, welcome card, prompt-chip, message, composer and split-pane component structure. `static/style.css` contains only worker-specific connection, status and MCP trace additions.

The full harness JavaScript and static directory are deliberately not included. Those files expect conversation storage, uploads, browser, desktop, pipeline and other private harness APIs that this federated worker does not own. The worker keeps its small Codex loop and sends all business tool calls through Datum Sync.

The inspection pane has two fixed presentations and three views: Artifacts, Run trace and Authority. It opens as a 360px split pane beside the conversation; the corners button expands that same live pane into a large, centred card. The active tab and trace content survive the transition. Use the restore button, click the backdrop, or press Escape to return to the split. There is no drag-resize state to manage.

## Worker cards

The worker sidebar opens native, read-only cards for the active agent session, MCP calls from that browser session, agent-visible managed credentials and proxies, and the selected principal bundle. The cards never receive upstream secrets or operator-wide fleet records. Use the Datum Sync portal when changing servers, tools, grants, principal state or reviewing the durable fleet audit.


## Governed artifacts

Agents can save bounded text, Markdown, HTML, JSON, GeoJSON and CSV output through Datum Sync's `resources_create` MCP tool. Every resource is versioned, private to its creating agent by default and stored with a content hash, source trace, expiry and owner. `resources_share` grants time-bounded read access to one active agent in the same fleet; `resources_revoke` removes it immediately.

The worker polls only metadata. A new visible resource opens in the Artifacts pane. HTML is rendered in a sandboxed iframe with no same-origin access or network connections; other supported formats are fetched on selection and inserted as text. Artifact content is never added to the model conversation automatically. The operator portal's Resources page shows ownership, active shares and payload-free lifecycle events and can revoke a sharing grant.
