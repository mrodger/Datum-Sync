"""SCIMAC site plan summary.

Reference workspace. Produces its outputs from parameters alone so that the
loader and job engine can be exercised before the connection store exists.
"""
import html
import json


async def run(params, emit, connections):
    job_id = params["JOB_ID"]
    fmt = params["OUTPUT_FORMAT"]
    include_notes = params["INCLUDE_NOTES"]

    await emit("progress", {"pct": 0.0, "message": f"Starting site plan for {job_id}"})

    summary = {
        "job_id": job_id,
        "format": fmt,
        "notes_included": include_notes,
        "sheets": ["Aerial", "Slope", "Elevation"],
    }

    await emit("progress", {"pct": 0.5, "message": "Composing report"})

    notes_block = (
        "<h2>Surveyor notes</h2><p>No notes recorded.</p>" if include_notes else ""
    )
    report = (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>Site plan {html.escape(job_id)}</title></head><body>"
        f"<h1>Site plan &mdash; job {html.escape(job_id)}</h1>"
        f"<p>Sheets: {', '.join(summary['sheets'])}</p>"
        f"{notes_block}"
        "</body></html>"
    )

    await emit("progress", {"pct": 1.0, "message": "Done"})

    return [
        {"name": "report", "type": "text/html", "content": report},
        {
            "name": "summary",
            "type": "application/json",
            "content": json.dumps(summary, indent=2),
        },
    ]
