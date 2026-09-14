# Local demonstration stack

Everything here drives one checkout's loopback prototype: the auth portal
(`datum_sync.portal`, 127.0.0.1:8210), the federated worker (`worker/`,
:8220), the read-only legacy MCP adapter (`datum_sync.legacy_mcp`, :8211), the
demo Office/PostgreSQL/harness MCP providers (`datum_sync.demo_mcp`, :8212), and
a private PostgreSQL 16 cluster on port 55435 under `.runtime/`.

| Script | Purpose |
|---|---|
| `manage.py init\|start\|stop\|restart\|status\|test` | Create and run the whole stack; `test` runs the focused portal and worker suites against a disposable database. |
| `provision_personas.py` | Clean browser-test data and register the personas in `personas.py` as governed agents. |
| `personas.py` | The seven demo personas (vendored from the Datum Harness reference UI); also read by `datum_sync.demo_mcp`. |
| `agent_demo.py` | A model-free MCP client for walking the governed tool flow without a worker. |
| `verify.py`, `check_schema.py` | Database checks against this checkout's disposable test database, never the live one. |
| `browser_smoke.py`, `source_ui_check.py` | Playwright checks of the portal and the served `static-v2` assets. |

`manage.py start` writes generated secrets (database password, the four MCP
provider tokens) to `.runtime/config.json` and the operator sign-in to
`.runtime/operator.txt`. `.runtime/` is gitignored; nothing in it belongs in a
commit.

The launcher expects the Ubuntu layout it was built on: a `.venv/` at the
repository root (it falls back to the current interpreter), and PostgreSQL 16
binaries plus GEOS/PROJ under `~/.local/share/datum-tools/root` as listed in
`system-packages.txt`. `requirements.lock.txt` is the Python environment that
produced the recorded test baseline. On another platform, run the portal and
worker by hand against the `docker-compose.yml` database instead.
