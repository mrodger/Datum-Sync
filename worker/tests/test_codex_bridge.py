from pathlib import Path
from codex_bridge import CodexBridge


def bridge():
    return CodexBridge(token="agent-token", datum_url="http://127.0.0.1:8210", model="gpt-5.6-luna", cwd=Path("/tmp/empty"), codex=Path("/bin/codex"))


def test_command_exposes_only_datum_mcp_and_disables_local_tools():
    joined = " ".join(bridge().command())
    assert "features.shell_tool=false" in joined
    assert "features.unified_exec=false" in joined
    assert "tools.web_search=false" in joined
    assert "mcp_servers={datum_sync=" in joined
    assert "DATUM_SYNC_TOKEN" in joined
    assert "agent-token" not in joined


def test_persona_prompt_extends_governance_instructions():
    item = CodexBridge(token="agent-token", datum_url="http://127.0.0.1:8210",
        model="gpt-5.6-luna", cwd=Path("/tmp/empty"), codex=Path("/bin/codex"),
        persona={"system_prompt": "You are Datum's GIS Analyst AI."})
    instructions = item.developer_instructions()
    assert "Datum Sync owns tool" in instructions
    assert instructions.endswith("You are Datum's GIS Analyst AI.")


def test_mcp_events_are_sanitized():
    event = bridge().normalize_notification("item/completed", {"turnId": "turn-1", "item": {"type": "mcpToolCall", "server": "datum_sync", "tool": "postgres__sample_orders", "status": "completed", "durationMs": 17, "arguments": {"secret": "must-not-leak"}, "result": {"rows": [1, 2]}}}, "turn-1")
    assert event == {"type": "tool", "phase": "completed", "server": "datum_sync", "tool": "postgres__sample_orders", "status": "completed", "duration_ms": 17, "has_error": False}


def test_other_codex_items_are_not_forwarded():
    event = bridge().normalize_notification("item/started", {"turnId": "turn-1", "item": {"type": "commandExecution", "command": "cat ~/.ssh/id_rsa"}}, "turn-1")
    assert event is None


def test_artifact_proxy_response_type_is_available():
    from app import Response
    result = Response(content=b"artifact", media_type="text/plain")
    assert result.body == b"artifact"
