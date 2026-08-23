# Datum-Sync — Workspace Contract

## Directory structure

Every workspace is a directory containing at minimum:

```
site_plan/
  main.py           ← required: entry point
  manifest.json     ← required: interface declaration
  MANIFEST.md       ← required: human-readable documentation
  requirements.txt  ← optional: workspace-level dependencies
```

---

## manifest.json schema

```json
{
  "name": "site_plan",
  "description": "Generates a geotechnical site plan for a Stratum job",
  "version": "1.0.0",
  "parameters": [
    {
      "name": "JOB_ID",
      "type": "STRING",
      "required": true,
      "description": "Stratum job number (e.g. 70023)",
      "group": "Input"
    },
    {
      "name": "OUTPUT_FORMAT",
      "type": "LOOKUP_CHOICE",
      "default": "HTML",
      "choices": ["HTML", "PDF", "JSON"],
      "group": "Output"
    }
  ],
  "outputs": [
    {"name": "report",     "type": "text/html",   "primary": true},
    {"name": "site_plan",  "type": "image/jpeg"}
  ],
  "services": ["job_submitter", "data_streaming", "data_download"],
  "connections": [
    {"name": "StratumDB_read", "access": "read"},
    {"name": "GDriveDelivery", "access": "write"}
  ],
  "timeout_seconds": 300
}
```

---

## main.py contract

```python
async def run(params: dict, emit, connections: dict) -> list[dict]:
    """
    params:      validated published parameters (types coerced per manifest)
    emit:        async callable — emit("event_type", payload_dict)
    connections: resolved connection objects keyed by connection name

    Returns a list of artifact dicts. Each artifact has at minimum:
      name  — matches an output declared in manifest.json
      type  — MIME type or service/* type

    Artifact content is one of:
      content  — inline string or bytes
      path     — absolute path to a file on disk (datum-sync copies it)
    """
    await emit("progress", {"pct": 0.0, "message": "Starting"})

    db = connections["StratumDB_read"]
    # ... do work ...

    await emit("progress", {"pct": 1.0, "message": "Done"})

    return [
        {"name": "report",    "type": "text/html",   "content": "<html>..."},
        {"name": "site_plan", "type": "image/jpeg",  "path": "/tmp/plan.jpg"},
    ]
```

---

## emit event types

| Type | Payload | Notes |
|---|---|---|
| `progress` | `{"pct": 0.0–1.0, "message": "..."}` | Displayed in UI and streamed via SSE |
| `log` | `{"level": "info|warn|error", "message": "..."}` | Written to job log |
| `artifact` | `{"name": "...", "type": "...", "content": "..."}` | Emit artifact mid-run (streaming) |

---

## MANIFEST.md required sections

```markdown
# <workspace name>

## Purpose
One paragraph. What it does and what it does NOT do.

## Dependencies
What it calls (connections, external services), with failure behaviour if unavailable.

## Dependents
What calls it, and the contract it expects.

## Failure modes
How it fails, status codes, recovery path.

## Behavioral contracts
Side effects, state changes, retry semantics.
```

---

## Publish gate

Run via `datum-sync publish {repo}/{workspace}`. All checks must pass:

1. `manifest.json` validates against schema (required fields, valid types, valid choices)
2. All declared connections exist in the connection store
3. Service account running the publish has `max_tier >= connection.tier` for all connections
4. `MANIFEST.md` is present and contains all required sections (non-empty)
5. Optional: `python main.py --smoke` exits 0 within 30 seconds

On pass: workspace is registered in DB and becomes callable.
On fail: specific check reported, workspace remains unpublished.

---

## Workspace isolation

Each workspace run executes in a subprocess with:
- Working directory set to the workspace directory
- Environment stripped of datum-sync internals
- Connections injected as resolved objects (credentials never in environment)
- `timeout_seconds` enforced via SIGTERM then SIGKILL
- stdout/stderr captured to job log
