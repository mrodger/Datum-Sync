"""Persona configuration for Datum UI."""

from pathlib import Path

_VAULT_PERSONAS = Path.home() / "vault" / "personas"

PERSONAS = [
    {
        "id": "manager",
        "display": "Manager",
        "role": "Project governance, delegation, status reporting.",
        "icon": "ph-kanban",
        "colour": "#8e7cc3",
        "default_pane_mode": "artifacts",
        "prompts": [
            "Give me an executive status summary of current jobs and outstanding work.",
            "Compare regional order activity and flag anything that needs attention.",
            "Review the available Office documents and prepare a concise project briefing.",
        ],
    },
    {
        "id": "developer",
        "display": "Developer",
        "role": "Code, architecture, debugging, deployment.",
        "icon": "ph-terminal-window",
        "colour": "#6b9dd1",
        "default_pane_mode": "code",
        "prompts": [
            "Inspect the available repositories and workspaces, then summarize their current job status.",
            "Describe the demo database schema and show a safe sample of recent orders.",
            "Investigate any failed jobs using status, events and logs, then suggest next steps.",
        ],
    },
    {
        "id": "researcher",
        "display": "Researcher",
        "role": "Deep research, analysis, knowledge synthesis.",
        "icon": "ph-magnifying-glass",
        "colour": "#d4a574",
        "default_pane_mode": "artifacts",
        "prompts": [
            "Resolve Crassostrea gigas and Perna canaliculus with WoRMS, compare their classifications, and use the persona catalogue to recommend who should continue the work.",
            "Review the quarterly brief and test its claims against the available operations data.",
            "Create a Wellington Leaflet map and explain which evidence you would add next.",
        ],
    },
    {
        "id": "designer",
        "display": "Designer",
        "role": "UI/UX, visual design, brand systems.",
        "icon": "ph-pen-nib",
        "colour": "#a8d5ba",
        "default_pane_mode": "artifacts",
        "prompts": [
            "Review the platform demo deck and propose a clearer six-slide narrative.",
            "Create a Wellington map with labelled points for a product walkthrough.",
            "Review the available personas and propose a simple visual identity for each.",
        ],
    },
    {
        "id": "security",
        "display": "Security",
        "role": "Threat modelling, auditing, hardening.",
        "icon": "ph-shield-check",
        "colour": "#d97d7d",
        "default_pane_mode": "code",
        "prompts": [
            "Audit the exposed MCP surface and summarize which systems, tools and schemas are visible.",
            "Review recent job events and logs for failures without exposing payloads.",
            "Explain how revoking one database tool changes this agent's effective access.",
        ],
    },
    {
        "id": "gis-analyst",
        "display": "GIS Analyst",
        "role": "Geospatial data, mapping, spatial analysis.",
        "icon": "ph-globe-hemisphere-west",
        "colour": "#7ba878",
        "default_pane_mode": "map",
        "prompts": [
            "Create a Wellington map and add labelled points for field operations.",
            "Resolve two New Zealand marine species with WoRMS and summarize their environments.",
            "Combine regional order statistics with current job status into a spatial operations brief.",
        ],
    },
    {
        "id": "data-enginer",
        "display": "Data Enginer",
        "role": "Data pipelines, spatial ETL, schema mapping and governed delivery.",
        "icon": "ph-flow-arrow",
        "colour": "#e07b39",
        "default_pane_mode": "artifacts",
        "prompts": [
            "Inspect the orders schema and propose a governed transformation pipeline.",
            "Summarize job health and identify failed or incomplete processing stages.",
            "Validate the fleet workbook and create a Wellington map for the output.",
        ],
    },
]

PERSONA_MAP = {p["id"]: p for p in PERSONAS}


def get_all() -> list[dict]:
    return PERSONAS


def get(persona_id: str) -> dict | None:
    return PERSONA_MAP.get(persona_id)


def get_system_prompt(persona_id: str) -> str | None:
    """Load the persona's vault definition file as a system prompt.

    Returns the markdown text of ~/vault/personas/<id>.md, or None if the
    file is missing. The caller is responsible for appending this to the
    Claude CLI invocation (via --append-system-prompt).
    """
    if not persona_id:
        return None
    p = _VAULT_PERSONAS / f"{persona_id}.md"
    if not p.is_file():
        return None
    try:
        return p.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None
