"""Small, explicit client for Datum Sync's governed MCP endpoint."""
from __future__ import annotations

from dataclasses import dataclass
import json

import httpx


class DatumSyncError(RuntimeError):
    pass


@dataclass(frozen=True)
class ToolOutcome:
    text: str
    trace_id: str | None
    is_error: bool


class DatumSyncMCP:
    """One short-lived MCP session authenticated by a Datum Sync OAuth token."""

    def __init__(self, base_url: str, access_token: str, *, transport=None):
        self.base_url = base_url.rstrip("/")
        self.access_token = access_token
        self.transport = transport
        self.client: httpx.AsyncClient | None = None
        self.session_id: str | None = None
        self._request_id = 0

    async def __aenter__(self):
        self.client = httpx.AsyncClient(
            base_url=self.base_url,
            headers={"Authorization": "Bearer " + self.access_token},
            timeout=20,
            follow_redirects=False,
            trust_env=False,
            transport=self.transport,
        )
        initialized = await self._rpc("initialize", {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "datum-federated-worker", "version": "0.1.0"},
        }, session_required=False)
        if initialized[0].get("serverInfo", {}).get("name") != "datum-auth-portal":
            raise DatumSyncError("unexpected MCP server identity")
        assert self.client is not None
        response = initialized[1]
        self.session_id = response.headers.get("mcp-session-id")
        if not self.session_id:
            raise DatumSyncError("Datum Sync did not return an MCP session")
        self.client.headers["Mcp-Session-Id"] = self.session_id
        await self.client.post("/mcp", json={
            "jsonrpc": "2.0", "method": "notifications/initialized",
        })
        return self

    async def __aexit__(self, _exc_type, _exc, _tb):
        if self.client:
            if self.session_id:
                try:
                    await self.client.delete("/mcp")
                except httpx.HTTPError:
                    pass
            await self.client.aclose()

    async def _rpc(self, method: str, params: dict, *, session_required=True):
        if not self.client:
            raise DatumSyncError("MCP session is not open")
        if session_required and not self.session_id:
            raise DatumSyncError("MCP session is not initialized")
        self._request_id += 1
        try:
            response = await self.client.post("/mcp", json={
                "jsonrpc": "2.0", "id": self._request_id,
                "method": method, "params": params,
            })
            response.raise_for_status()
            body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise DatumSyncError("Datum Sync MCP request failed") from exc
        if "error" in body:
            message = body["error"].get("message", "MCP error")
            raise DatumSyncError(str(message)[:300])
        if "result" not in body:
            raise DatumSyncError("Datum Sync returned an invalid MCP response")
        return body["result"], response

    async def tools(self) -> list[dict]:
        result, _ = await self._rpc("tools/list", {})
        items = result.get("tools")
        if not isinstance(items, list):
            raise DatumSyncError("Datum Sync returned an invalid tool catalogue")
        output = []
        for item in items:
            if not isinstance(item, dict) or not isinstance(item.get("name"), str):
                continue
            output.append({
                "type": "function",
                "function": {
                    "name": item["name"],
                    "description": str(item.get("description", ""))[:1000],
                    "parameters": item.get("inputSchema") or {"type": "object"},
                },
            })
        return output

    async def call(self, name: str, arguments: dict) -> ToolOutcome:
        result, response = await self._rpc("tools/call", {
            "name": name, "arguments": arguments,
        })
        structured = result.get("structuredContent")
        if structured is not None:
            text = json.dumps(structured, default=str)
        else:
            parts = result.get("content") or []
            text = "\n".join(
                str(part.get("text", "")) for part in parts
                if isinstance(part, dict) and part.get("type") == "text"
            ) or "[no output]"
        return ToolOutcome(
            text=text[:50_000],
            trace_id=response.headers.get("x-trace-id"),
            is_error=bool(result.get("isError")),
        )
