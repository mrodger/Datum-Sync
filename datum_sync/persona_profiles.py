"""Initial agent bundles derived from the Datum Harness persona metadata."""

BASE_VOICE = (
    "Be terse and direct. No filler. Answer first, explain if needed. "
    "No emojis unless asked. Use markdown formatting where appropriate."
)

LEGACY_METADATA = [
    "legacy__list_repositories", "legacy__list_workspaces", "legacy__workspace_manifest",
]
LEGACY_JOBS = [
    "legacy__job_summary", "legacy__list_jobs", "legacy__job_status",
    "legacy__job_log", "legacy__job_events", "legacy__job_artifacts",
]
POSTGRES_READ = [
    "postgres__list_schemas", "postgres__list_tables", "postgres__describe_table",
    "postgres__sample_orders", "postgres__orders_summary",
]
OFFICE_READ = ["office__officecli"]
HARNESS_PERSONAS = ["harness__list_personas"]
HARNESS_SPATIAL = ["harness__render_leaflet_map", "harness__worms_lookup"]

PERSONA_TOOLS = {
    "manager": {
        "legacy-local": LEGACY_METADATA + LEGACY_JOBS,
        "officecli-demo": OFFICE_READ,
        "postgres-demo": POSTGRES_READ,
        "harness-tools": HARNESS_PERSONAS,
    },
    "developer": {
        "legacy-local": LEGACY_METADATA + LEGACY_JOBS,
        "postgres-demo": POSTGRES_READ,
        "harness-tools": HARNESS_PERSONAS,
    },
    "researcher": {
        "officecli-demo": OFFICE_READ,
        "postgres-demo": ["postgres__sample_orders", "postgres__orders_summary"],
        "harness-tools": HARNESS_PERSONAS + HARNESS_SPATIAL,
    },
    "designer": {
        "officecli-demo": OFFICE_READ,
        "harness-tools": HARNESS_PERSONAS + ["harness__render_leaflet_map"],
    },
    "security": {
        "legacy-local": LEGACY_METADATA + LEGACY_JOBS,
        "postgres-demo": ["postgres__list_schemas", "postgres__list_tables", "postgres__describe_table"],
        "harness-tools": HARNESS_PERSONAS,
    },
    "gis-analyst": {
        "legacy-local": LEGACY_METADATA + LEGACY_JOBS,
        "postgres-demo": POSTGRES_READ,
        "harness-tools": HARNESS_PERSONAS + HARNESS_SPATIAL,
    },
    "data-enginer": {
        "legacy-local": LEGACY_METADATA + LEGACY_JOBS,
        "officecli-demo": OFFICE_READ,
        "postgres-demo": POSTGRES_READ,
        "harness-tools": HARNESS_PERSONAS + ["harness__render_leaflet_map"],
    },
}

WRITERS = {"manager", "developer", "designer", "gis-analyst", "data-enginer"}
PUBLISHERS = {"manager", "designer", "gis-analyst", "data-enginer"}


def system_prompt(persona: dict) -> str:
    return (
        f"You are Datum's {persona['display']} AI. "
        f"Your focus: {persona['role']} {BASE_VOICE}"
    )


def grant_for(persona_id: str) -> dict:
    if persona_id not in PERSONA_TOOLS:
        raise KeyError(persona_id)
    can_write = persona_id in WRITERS
    can_publish = persona_id in PUBLISHERS
    return {
        "repositories": ["Testing"],
        "read": ["demo/**"],
        "write": ["demo/**"] if can_write else [],
        "publish": ["demo/**"] if can_publish else [],
        "deny": ["private/**"],
        "sessions": 1,
        "mcp": {
            connection: {"tools": {"allow": list(tools), "deny": []}}
            for connection, tools in PERSONA_TOOLS[persona_id].items()
        },
    }
