"""Durable, payload-free lifecycle records for the auth portal MCP endpoint."""
from __future__ import annotations

import json
import time
from typing import Any

from datum_sync import db

CHANNEL = "mcp_flow_events"
_MAX_DETAIL_BYTES = 2048
_dropped = 0


def dropped() -> int:
    return _dropped


def _short(value: Any, limit: int) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if len(text) <= limit else text[:limit] + "...[truncated]"


def rpc_id(value: Any) -> str | None:
    if value is None:
        return None
    try:
        return _short(json.dumps(value, separators=(",", ":"), default=str), 128)
    except Exception:
        return "[unserializable]"


def _detail(value: dict[str, Any] | None) -> str:
    encoded = json.dumps(value or {}, separators=(",", ":"), default=str)
    if len(encoded.encode()) <= _MAX_DETAIL_BYTES:
        return encoded
    return json.dumps({"truncated": True, "keys": sorted((value or {}).keys())})


async def _event(conn, trace_id, kind: str, detail: dict[str, Any] | None = None) -> int:
    event_id = await conn.fetchval(
        "INSERT INTO mcp_flow_events(trace_id,kind,detail) VALUES($1,$2,$3) RETURNING id",
        trace_id, _short(kind, 80), _detail(detail),
    )
    await conn.execute(
        "SELECT pg_notify($1,$2)", CHANNEL,
        json.dumps({"event_id": event_id, "trace_id": str(trace_id), "kind": kind}),
    )
    return event_id


async def start(trace_id, *, http_method: str, request_bytes: int) -> float:
    """Create the flow before parsing or authentication. Never raises."""
    global _dropped
    started = time.monotonic()
    try:
        async with db.pool().acquire() as conn, conn.transaction():
            await conn.execute(
                """INSERT INTO mcp_flows(trace_id,http_method,request_bytes)
                   VALUES($1,$2,$3)""",
                trace_id, _short(http_method, 16), max(0, request_bytes),
            )
            await _event(conn, trace_id, "request.received",
                         {"http_method": _short(http_method, 16), "request_bytes": max(0, request_bytes)})
    except Exception:
        _dropped += 1
    return started


async def mark(
    trace_id,
    kind: str,
    *,
    conn=None,
    rpc_method: str | None = None,
    request_id: str | None = None,
    tool_name: str | None = None,
    principal=None,
    session_id=None,
    client_name: str | None = None,
    provider: str | None = None,
    connection_name: str | None = None,
    upstream_tool: str | None = None,
    detail: dict[str, Any] | None = None,
) -> None:
    """Append one checkpoint and update safe flow dimensions. Never raises."""
    global _dropped

    async def write(target):
        # The savepoint prevents a logging error from aborting a caller-owned
        # authority transaction.
        async with target.transaction():
            await target.execute(
                """UPDATE mcp_flows SET
                     rpc_method=COALESCE($2,rpc_method),
                     rpc_id=COALESCE($3,rpc_id),
                     tool_name=COALESCE($4,tool_name),
                     principal_id=COALESCE($5,principal_id),
                     principal_name=COALESCE($6,principal_name),
                     credential_id=COALESCE($7,credential_id),
                     credential_kind=COALESCE($8,credential_kind),
                     session_id=COALESCE($9,session_id),
                     client_name=COALESCE($10,client_name),
                     provider=COALESCE($11,provider),
                     connection_name=COALESCE($12,connection_name),
                     upstream_tool=COALESCE($13,upstream_tool)
                   WHERE trace_id=$1""",
                trace_id,
                _short(rpc_method, 80),
                _short(request_id, 128),
                _short(tool_name, 200),
                principal.id if principal else None,
                _short(principal.name, 200) if principal else None,
                principal.credential["id"] if principal else None,
                _short(principal.credential["kind"], 40) if principal else None,
                session_id,
                _short(client_name, 80),
                provider,
                _short(connection_name, 200),
                _short(upstream_tool, 200),
            )
            await _event(target, trace_id, kind, detail)

    try:
        if conn is not None:
            await write(conn)
        else:
            async with db.pool().acquire() as owned:
                await write(owned)
    except Exception:
        _dropped += 1


async def finish(
    trace_id,
    *,
    started: float,
    outcome: str,
    response_bytes: int,
    error_code: str | int | None = None,
    detail: dict[str, Any] | None = None,
) -> None:
    """Finalize the summary and append its terminal event. Never raises."""
    global _dropped
    duration_ms = max(0, int((time.monotonic() - started) * 1000))
    terminal = {
        "ok": "request.completed",
        "denied": "request.denied",
        "tool_error": "request.tool_error",
        "protocol_error": "request.protocol_error",
        "upstream_error": "request.upstream_error",
        "error": "request.failed",
    }.get(outcome, "request.failed")
    try:
        async with db.pool().acquire() as conn, conn.transaction():
            await conn.execute(
                """UPDATE mcp_flows SET outcome=$2,error_code=$3,response_bytes=$4,
                     duration_ms=$5,completed_at=now() WHERE trace_id=$1""",
                trace_id, outcome, _short(error_code, 80), max(0, response_bytes), duration_ms,
            )
            await _event(conn, trace_id, terminal,
                         {"outcome": outcome, "error_code": _short(error_code, 80),
                          "response_bytes": max(0, response_bytes), "duration_ms": duration_ms,
                          **(detail or {})})
    except Exception:
        _dropped += 1
