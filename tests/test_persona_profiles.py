from datetime import datetime, timedelta, timezone
import uuid

from datum_sync.persona_profiles import PERSONA_TOOLS, grant_for, system_prompt
from datum_sync.portal_core import Identity, grant_valid


def test_persona_bundles_are_distinct_valid_and_bounded():
    assert set(PERSONA_TOOLS) == {
        "manager", "developer", "researcher", "designer",
        "security", "gis-analyst", "data-enginer",
    }
    grants = {name: grant_valid(grant_for(name)) for name in PERSONA_TOOLS}
    signatures = {name: tuple(sorted(grant["mcp"])) for name, grant in grants.items()}
    assert signatures["designer"] != signatures["security"]
    assert "harness__worms_lookup" in grants["researcher"]["mcp"]["harness-tools"]["tools"]["allow"]
    assert "postgres__sample_orders" not in grants["security"]["mcp"]["postgres-demo"]["tools"]["allow"]
    assert grants["researcher"]["write"] == []
    assert grants["manager"]["publish"] == ["demo/**"]
    assert grants["data-enginer"]["mcp"]["officecli-demo"]["tools"]["allow"]


def test_persona_metadata_is_returned_to_its_authenticated_worker():
    persona = {
        "id": "gis-analyst", "display": "GIS Analyst", "role": "Geospatial analysis.",
        "system_prompt": "You are Datum's GIS Analyst AI.", "definition_complete": False,
        "starter_prompts": ["Create a map"], "bundle_version": 1,
    }
    now = datetime.now(timezone.utc)
    identity = Identity(
        {"id": 42, "name": "gis-analyst", "portal_kind": "agent", "portal_state": "active",
         "max_tier": 3, "is_admin": False, "portal_grant": grant_for("gis-analyst"),
         "portal_metadata": {"purpose": "GIS", "persona": persona}},
        {"id": uuid.uuid4(), "kind": "access", "scope": "mcp", "expires_at": now + timedelta(minutes=10)},
        [grant_for("gis-analyst")], 2,
    )
    public = identity.public()
    assert public["persona"]["display"] == "GIS Analyst"
    assert public["persona"]["system_prompt"] == persona["system_prompt"]
    assert "purpose" not in public["persona"]


def test_fallback_prompt_uses_available_harness_role():
    prompt = system_prompt({"display": "Security", "role": "Threat modelling and auditing."})
    assert prompt.startswith("You are Datum's Security AI.")
    assert "Threat modelling and auditing." in prompt
