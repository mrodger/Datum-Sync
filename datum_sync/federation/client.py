"""The gateway as an MCP client (spec/agent-auth-plane/05 §1, §3).

Streamable HTTP only: one `POST` per JSON-RPC message, the upstream's
`Mcp-Session-Id` carried back on the next. stdio upstreams are a process,
and this gateway spawns processes for jobs only.

What never crosses this boundary: the inbound caller's `Authorization` (the
outgoing headers are built from the connection alone, FED-011) and the
upstream's credential on the way back (`scrub` removes any secret value
from a result before it is returned). SSRF is `proxy.validate_upstream_url`
unless the connection was saved with `allow_private_origin`, which needs
tier 5 (`connections.validate`, FED-018).
"""
from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlparse

import httpx

from datum_sync import config as app_config, proxy
from datum_sync.errors import ApiError

PROTOCOL_VERSION = "2025-06-18"
CLIENT_INFO = {"name": "datum-sync", "version": "0.1.0"}

# Overridden by tests to point every upstream at an in-process ASGI app.
TRANSPORT: httpx.AsyncBaseTransport | None = None


class UpstreamError(Exception):
    """The upstream could not be reached or answered outside the protocol."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class ToolError(Exception):
    """The upstream answered a JSON-RPC error for a well-formed call."""

    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


def validate_url(url: str, allow_private: bool) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ApiError(400, "INVALID_URL", "upstream url must be http(s) with a host")
    if allow_private:
        return url
    return proxy.validate_upstream_url(url)


def scrub(text: str, secrets: list[str]) -> str:
    """The upstream's own credential never appears in what a caller gets."""
    for s in secrets:
        if s and len(s) >= 6 and s in text:
            text = text.replace(s, "[redacted]")
    return text


def _http_client(timeout: float) -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=timeout, follow_redirects=False, transport=TRANSPORT)


class Upstream:
    """One connection's worth of client state: URL, headers, session."""

    def __init__(self, name: str, cfg: dict[str, Any], secret: dict[str, Any] | None) -> None:
        self.name = name
        self.cfg = cfg
        self.secret = secret or {}
        self.url = validate_url(cfg["url"], bool(cfg.get("allow_private_origin")))
        self.session_id: str | None = None
        self.timeout = float(cfg.get("timeout_seconds") or app_config.FEDERATION_CALL_TIMEOUT_SECONDS)
        self._next_id = 0

    @property
    def secret_values(self) -> list[str]:
        return [str(v) for v in self.secret.values() if isinstance(v, (str, int))]

    def headers(self) -> dict[str, str]:
        # From the connection only. The caller's own Authorization is not an
        # input here, and a static Authorization in `config.headers` is
        # dropped too: the credential belongs in `secret` (FED-011).
        out = {k: str(v) for k, v in (self.cfg.get("headers") or {}).items()
               if k.lower() != "authorization"}
        proxy.inject_auth(self.cfg, self.secret, out, {})
        out["Accept"] = "application/json, text/event-stream"
        out["MCP-Protocol-Version"] = PROTOCOL_VERSION
        if self.session_id:
            out["Mcp-Session-Id"] = self.session_id
        return out

    async def rpc(self, method: str, params: dict[str, Any] | None = None,
                  timeout: float | None = None) -> Any:
        self._next_id += 1
        body = {"jsonrpc": "2.0", "id": self._next_id, "method": method, "params": params or {}}
        try:
            async with _http_client(timeout or self.timeout) as client:
                r = await client.post(self.url, json=body, headers=self.headers())
        except httpx.HTTPError as exc:
            raise UpstreamError("UPSTREAM_UNAVAILABLE", f"{self.name}: {type(exc).__name__}: {exc}") from None
        if r.status_code == 404 and self.session_id:
            # The upstream forgot us; re-initialise once.
            self.session_id = None
            await self.initialize()
            return await self.rpc(method, params, timeout)
        if r.status_code >= 400:
            raise UpstreamError("UPSTREAM_ERROR", f"{self.name}: HTTP {r.status_code}")
        sid = r.headers.get("Mcp-Session-Id")
        if sid:
            self.session_id = sid
        if len(r.content) > app_config.FEDERATION_MAX_RESULT_BYTES * 2:
            raise UpstreamError("UPSTREAM_ERROR", f"{self.name}: response too large")
        message = _parse(r)
        if "error" in message:
            err = message["error"] or {}
            raise ToolError(int(err.get("code", -32000)), str(err.get("message", "error")), err.get("data"))
        return message.get("result")

    async def initialize(self) -> dict[str, Any]:
        result = await self.rpc("initialize", {
            "protocolVersion": PROTOCOL_VERSION, "capabilities": {}, "clientInfo": CLIENT_INFO,
        }, timeout=app_config.FEDERATION_LIST_TIMEOUT_SECONDS)
        # Notification: no id, nothing to read back. Best effort.
        try:
            async with _http_client(app_config.FEDERATION_LIST_TIMEOUT_SECONDS) as client:
                await client.post(self.url, json={"jsonrpc": "2.0", "method": "notifications/initialized"},
                                  headers=self.headers())
        except httpx.HTTPError:
            pass
        return result or {}

    async def tools_list(self) -> list[dict[str, Any]]:
        result = await self.rpc("tools/list", timeout=app_config.FEDERATION_LIST_TIMEOUT_SECONDS)
        return list((result or {}).get("tools") or [])

    async def resources_list(self, templates: bool = False) -> list[dict[str, Any]]:
        method = "resources/templates/list" if templates else "resources/list"
        try:
            result = await self.rpc(method, timeout=app_config.FEDERATION_LIST_TIMEOUT_SECONDS)
        except ToolError as exc:
            if exc.code == -32601:  # the upstream has no resources
                return []
            raise
        key = "resourceTemplates" if templates else "resources"
        return list((result or {}).get(key) or [])

    async def tools_call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        result = await self.rpc("tools/call", {"name": name, "arguments": arguments})
        return _cap_result(result or {}, self.secret_values)

    async def resources_read(self, uri: str) -> dict[str, Any]:
        result = await self.rpc("resources/read", {"uri": uri})
        return _cap_result(result or {}, self.secret_values)


def _parse(r: httpx.Response) -> dict[str, Any]:
    ctype = r.headers.get("content-type", "")
    if "text/event-stream" in ctype:
        last: dict[str, Any] | None = None
        for line in r.text.splitlines():
            if line.startswith("data:"):
                try:
                    msg = json.loads(line[5:].strip())
                except json.JSONDecodeError:
                    continue
                if isinstance(msg, dict) and ("result" in msg or "error" in msg):
                    last = msg
        if last is None:
            raise UpstreamError("UPSTREAM_ERROR", "no JSON-RPC response in the event stream")
        return last
    try:
        msg = r.json()
    except ValueError:
        raise UpstreamError("UPSTREAM_ERROR", "response is not JSON") from None
    if not isinstance(msg, dict):
        raise UpstreamError("UPSTREAM_ERROR", "response is not a JSON-RPC message")
    return msg


def _cap_result(result: dict[str, Any], secrets: list[str]) -> dict[str, Any]:
    """Content blocks verbatim, capped at 1 MiB and scrubbed; upstream _meta dropped."""
    out: dict[str, Any] = {}
    budget = app_config.FEDERATION_MAX_RESULT_BYTES
    blocks = []
    used = 0
    truncated = False
    for block in result.get("content") or result.get("contents") or []:
        if not isinstance(block, dict):
            continue
        block = dict(block)
        for key in ("text", "blob", "data"):
            if isinstance(block.get(key), str):
                text = scrub(block[key], secrets)
                if used + len(text) > budget:
                    text = text[: max(0, budget - used)] + "\n[truncated at 1 MiB by the gateway]"
                    truncated = True
                used += len(text)
                block[key] = text
        blocks.append(block)
        if truncated:
            break
    out["content" if "content" in result or "contents" not in result else "contents"] = blocks
    if isinstance(result.get("structuredContent"), (dict, list)):
        encoded = json.dumps(result["structuredContent"])
        if used + len(encoded) <= budget:
            out["structuredContent"] = json.loads(scrub(encoded, secrets))
    out["isError"] = bool(result.get("isError", False))
    return out
