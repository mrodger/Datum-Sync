# 05 — Workspace Contract

## 1. On disk

```
repositories/
  SCIMAC/
    site_plan/
      main.py           required — defines async run(params, emit, connections)
      manifest.json     required — the interface
      MANIFEST.md       required — five documented sections
      requirements.txt  optional — installed into the workspace venv at publish (§7)
      .dgignore         optional — paths excluded from the content hash
```

`REPOSITORIES_PATH` is the root. Repository and workspace names are directory
names and must match `02-domain-model.md §3`. `manifest.name` must equal the
directory name.

## 2. `manifest.json`

```json
{
  "name": "site_plan",
  "description": "Generates a geotechnical site plan for a Stratum job",
  "version": "1.2.0",
  "parameters": [
    {"name": "JOB_ID", "type": "STRING", "required": true,
     "description": "Stratum job number", "group": "Input", "pattern": "^[0-9]{4,6}$"},
    {"name": "OUTPUT_FORMAT", "type": "CHOICE", "default": "HTML",
     "choices": ["HTML", "PDF", "JSON"], "group": "Output"},
    {"name": "AREA", "type": "GEOMETRY", "required": false,
     "description": "Optional WKT polygon clipping the plan"},
    {"name": "SURVEY", "type": "FILE", "required": false, "accept": [".csv", ".zip"]}
  ],
  "outputs": [
    {"name": "report",    "type": "text/html", "primary": true},
    {"name": "site_plan", "type": "image/jpeg"},
    {"name": "viewer",    "type": "service/static", "service_name": "scimac-viewer"}
  ],
  "services": ["job_submitter", "data_streaming", "data_download", "mcp"],
  "connections": [
    {"name": "StratumDB", "access": "read"},
    {"name": "GDriveDelivery", "access": "write"}
  ],
  "timeout_seconds": 300,
  "max_attempts": 1,
  "smoke_test": true,
  "mcp": {
    "title": "Site plan",
    "description_for_agents": "Produce the site plan for a job. Returns HTML inline; the JPEG is linked.",
    "wait_default_seconds": 45
  }
}
```

### 2.1 Parameter types

| Type | Wire form | Coerced to | Notes |
|---|---|---|---|
| `STRING` | string | `str` | optional `pattern` (regex, anchored), `max_length` |
| `INTEGER` | string or int | `int` | optional `min`, `max`; booleans refused |
| `FLOAT` | string or number | `float` | optional `min`, `max` |
| `BOOLEAN` | string or bool | `bool` | `true/1/yes/on`, `false/0/no/off` |
| `CHOICE` | string | `str` | requires `choices`; default must be a choice |
| `FILE` | upload id (UUID) | absolute path (worker only) | optional `accept` extensions; never a path on the wire |
| `GEOMETRY` | WKT or GeoJSON string | validated WKT `str` | validated with PostGIS `ST_GeomFromText` / `ST_GeomFromGeoJSON` at submit; the one PostGIS use in the gateway |
| `JSON` | object | `dict` | optional `schema` (JSON Schema draft 2020-12), validated at submit |

Common fields: `name` (`^[A-Z][A-Z0-9_]{0,63}$`), `type`, `required`
(default false), `default`, `description`, `group`. A required parameter
MUST NOT declare a default. Unknown keys are refused.

### 2.2 Outputs

`name` unique, `type` a MIME type or `service/static|pwa|dashboard`.
Exactly one output is `primary` when there is more than one; it is what
`/stream` and `/download` return. A `service/*` output carries
`service_name`, the URL segment it will occupy — checked for collisions at
publish (`13-hosted-services.md`).

### 2.3 Services

Which surfaces expose this workspace:

| Value | Surface |
|---|---|
| `job_submitter` | `POST /rest/v1/jobs/submit/{repo}/{ws}` (async) |
| `data_streaming` | `GET /stream/{repo}/{ws}` (sync, primary output body) |
| `data_download` | `GET /download/{repo}/{ws}` (sync, attachment) |
| `data_upload` | `POST /upload/{repo}/{ws}` (multipart intake + submit) |
| `mcp` | Listed in `tools/list`; callable via `tools/call` |

`mcp` is explicit. A workspace with a **required** `FILE` parameter cannot
declare `mcp` (the gate refuses it) because an MCP client cannot perform the
upload; optional `FILE` parameters are simply omitted from the tool's schema.

### 2.4 Connections

Declared by name with the access the workspace needs. Enforced at publish
against the connection's stored `access` and scope, and against the
**publisher's** grant (`connections.use` and tier).

### 2.5 `timeout_seconds`, `max_attempts`

Timeout is enforced by the runner with SIGTERM then SIGKILL on the process
group. `max_attempts` (default 1, max 5) is how many times the engine will
run the job if the *worker* dies mid-run — never on the workspace's own
failure. A workspace that fails has failed.

### 2.6 `mcp` block

Optional presentation hints for agents. `wait_default_seconds` is how long
`tools/call` blocks before returning a job handle instead of a result
(`11-mcp.md §5`).

## 3. `main.py`

```python
async def run(params: dict, emit, connections: dict) -> list[dict]:
    """
    params       validated, coerced parameters
    emit         async emit(event_type: str, payload: dict) -> None
    connections  name -> resolved connection object (dict), 07 §5
    returns      list of artifact descriptors, each with:
                   name     matches a declared output
                   content  str | bytes        (exactly one of content / path)
                   path     absolute path to a file, or a directory for service/* outputs
    """
```

The child harness also exposes `run_context()` returning
`{job_id, trace_id, artifact_dir, workspace_dir, submitted_by, attempt}` for
workspaces that want it. Nothing else is importable from `datumgate` inside
a workspace — the child does not have the database URL, and the package on
`PYTHONPATH` is the child harness only.

### 3.1 Events

| `event_type` | payload | Effect |
|---|---|---|
| `progress` | `{"pct": 0.0–1.0, "message": str}` | Announced on the job's SSE stream; not stored |
| `log` | `{"level": "debug|info|warn|error", "message": str}` | Appended to `job_log` |
| `metric` | `{"name": str, "value": number, "unit": str?}` | Appended to `job_log` as `info` with structured prefix `metric name=… value=…`; surfaced by the UI as a chip |
| `artifact` | same as a returned artifact | Stored immediately (streaming outputs); reconciled with the return list at the end — duplicates by name are an error |

`print()` and stderr inside the workspace go to `job_log` at `info`; they
cannot corrupt the event channel because fd 1 is redirected before the
workspace is imported (`06-job-engine.md §6`).

### 3.2 Smoke test

If `smoke_test` is true, `python main.py --smoke` must exit 0 within 30
seconds. A smoke handler must not perform the workspace's real side effects.
Opt-in because the gateway cannot tell whether a script honours the flag.

## 4. `MANIFEST.md`

Five `##` sections, all non-empty (a section containing only `N/A`, `TBD`,
`TODO`, dashes, ellipses or an HTML comment is empty):

```
# <workspace name>
## Purpose
## Dependencies
## Dependents
## Failure modes
## Behavioral contracts
```

Stored verbatim in `workspace_versions.doc_text` and shown in the UI and as
the MCP tool's long description (first paragraph of Purpose).

## 5. Publish gate

`python -m datumgate sync [--repository R] [--dry-run] [--prune] [--as PRINCIPAL]`
and `POST /rest/v1/repositories/{repo}/sync` (tier ≥4, repo in scope).

For each workspace directory, checks run cheapest-first and stop at the first
failure. Failure leaves the previous current version untouched.

| # | Check | Module |
|---|---|---|
| 1 | Directory has `main.py`, `manifest.json`, `MANIFEST.md` | `repository.py` |
| 2 | `manifest.json` parses and validates; `name` matches directory | `manifest.py` |
| 3 | `main.py` parses (`ast`) and defines `async def run(params, emit, connections)` — **parsed, never imported** | `repository.py` |
| 4 | `MANIFEST.md` has all five sections non-empty | `publish.py` |
| 5 | Content hash computed (§6); if equal to the current version's hash and no `--force`, the workspace is **unchanged**: skip remaining checks, report `unchanged` | `publish.py` |
| 6 | Every declared connection exists, its scope covers `repo/ws`, its `access` satisfies the declared access, its tier ≤ publisher tier, and its name is in publisher `connections.use` | `publish.py` |
| 7 | Every `service/*` output has a `service_name`, the type is servable, and the name is unowned or owned by this same `repo/ws` | `publish.py` |
| 8 | `mcp` in services ⇒ no required `FILE` parameter | `publish.py` |
| 9 | If `requirements.txt` present: the workspace venv is built/updated (§7) | `publish.py` |
| 10 | If `smoke_test`: `main.py --smoke` exits 0 in ≤30s, in a subprocess, using the workspace venv, with the stripped environment | `publish.py` |

On pass, in one transaction:

1. `INSERT workspace_versions (…, status='active')`
2. previous current version → `status='superseded'`
3. `UPDATE workspaces SET current_version_id = new`
4. audit `workspace.publish`

`--prune` deregisters workspaces whose directory is gone: current version →
`withdrawn`, `current_version_id = NULL`, owned hosted services deleted.
Queued jobs pinned to a withdrawn version fail at claim with a clear error.

Publishing as the CLI without `--as` acts as `system:local` (tier 5). With
`--as NAME` it acts as that principal's effective grant — the way a
restricted publisher is tested.

## 6. Content hash

```
sha256 over, in sorted path order, for each file not excluded by .dgignore:
    relative_path + "\0" + file_size + "\0" + sha256(file_bytes) + "\n"
```

`requirements.txt` is included; `__pycache__`, `.venv`, `*.pyc` are always
excluded. The hash is what makes publish idempotent and versions
content-addressed: re-running `sync` on an unchanged tree writes nothing.

## 7. Workspace dependencies

If `requirements.txt` exists, publish creates or updates a venv at
`DATA_PATH/venvs/{repo}/{ws}/{sha256(requirements.txt)[:16]}` using
`python -m venv --system-site-packages` followed by `pip install -r`. The
version row records the venv path in `manifest._venv` (an internal key the
gate adds; not allowed in user manifests). The runner launches the child with
that venv's interpreter. Workspaces without `requirements.txt` run under the
gateway's interpreter, as the original does.

Venvs are never deleted by publish; the retention sweeper removes venvs no
active or superseded version references after `retention.venv_days`.

## 8. Isolation

Each run executes in a subprocess:

- `cwd` = the workspace directory
- environment = `PATH HOME LANG LC_ALL TZ SSL_CERT_FILE` only, plus
  `PYTHONUNBUFFERED=1`, `PYTHONPATH=<child harness dir only>`,
  `DG_JOB_ID`, `DG_TRACE_ID`
- **no** `DATABASE_URL`, **no** secret key, **no** `.env`
- own session/process group; `PR_SET_PDEATHSIG=SIGKILL` on Linux
- spec (params, connections, paths) delivered on **stdin**, never argv
- optional `policy.sandbox` may wrap the command (`bwrap`, `firejail`,
  or a user-supplied wrapper array) — the runner treats it as an opaque
  prefix; no sandbox is the default

## 9. The seam for a future workflow builder

A visual builder (separate project) will need to run graphs of steps. The
gateway's stable seam is:

- a workspace is the unit of execution; a builder compiles a graph to one
  workspace directory whose `main.py` orchestrates steps in-process, **or**
  to one automation chain (`run_workspace` actions with template-passed
  params). Both are supported today with no gateway change.
- `manifest.json` may carry an opaque `"x-builder": {...}` block that the
  gate stores and ignores (`x-` prefixed top-level keys are permitted and
  preserved; everything else unknown is refused).
- The REST endpoint `GET /rest/v1/workspaces/{repo}/{ws}/schema` returns
  the manifest as JSON Schema for parameters and a typed outputs list, which
  is what a builder needs to wire nodes.
