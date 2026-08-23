"""MCP Streamable HTTP endpoint. Published workspaces appear as tools.

One route, `POST /mcp`, speaking JSON-RPC 2.0. Stateless: no session id is
issued and none is required, because every request already carries a bearer
token that identifies the account. A session would be a second piece of state
saying the same thing, with its own expiry to get wrong.

`GET /mcp` returns 405. The transport permits a server to decline the
server-initiated SSE stream, and we have nothing to push: a tool call is
synchronous from the client's point of view.

Two deliberate mappings, both of which mean fewer tools rather than broken
ones:

  * A workspace appears only if it publishes the `data_streaming` service.
    `tools/call` runs it and returns the output, which is exactly what that
    service means, so MCP reuses the gate rather than inventing a service that
    every existing manifest would have to opt into.
  * A workspace with a **required FILE parameter is hidden**. An MCP client
    has no way to perform the upload that produces an upload id, so the tool
    could only ever be called wrongly. Advertising it would trade a missing
    tool for a tool that always fails.
"""
from __future__ import annotations

import json
import re
from typing import Any

import asyncpg
from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse

from datum_sync import auth, config, db, execute
from datum_sync.auth import Principal
from datum_sync.errors import ApiError
from datum_sync.manifest import Manifest, ParameterType

router = APIRouter()

PROTOCOL_VERSION = "2025-06-18"

SERVER_INFO = {"name": "datum-sync", "version": "0.1.0"}

# The service a workspace must publish to be reachable as a tool.
MCP_SERVICE = "data_streaming"

# Beyond this an artifact is linked rather than inlined. A multi-megabyte
# result pasted into a conversation is not usable by the model and displaces
# the context it needs to interpret it.
MAX_INLINE_BYTES = 64 * 1024

_UNSAFE_TOOL_CHAR = re.compile(r"[^A-Za-z0-9_-]")

# JSON-RPC 2.0 error codes.
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


class RpcError(Exception):
    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


# -- tool catalogue --------------------------------------------------------

_JSON_TYPE = {
    ParameterType.STRING: {"type": "string"},
    ParameterType.INTEGER: {"type": "integer"},
    ParameterType.FLOAT: {"type": "number"},
    ParameterType.BOOLEAN: {"type": "boolean"},
}


def tool_name(repo: str, ws: str) -> str:
    return _UNSAFE_TOOL_CHAR.sub("_", f"{repo}__{ws}")[:128]


def input_schema(manifest: Manifest) -> dict[str, Any]:
    properties: dict[str, Any] = {}
    required: list[str] = []
    for p in manifest.parameters:
        if p.type is ParameterType.FILE:
            # Optional only -- callable() below refuses the workspace outright
            # if a FILE parameter is required.
            continue
        if p.type is ParameterType.LOOKUP_CHOICE:
            schema: dict[str, Any] = {"type": "string", "enum": list(p.choices or [])}
        else:
            schema = dict(_JSON_TYPE[p.type])
        if p.description:
            schema["description"] = p.description
        if p.default is not None:
            schema["default"] = p.default
        properties[p.name] = schema
        if p.required:
            required.append(p.name)
    out: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        out["required"] = required
    return out


def callable_by_mcp(manifest: Manifest) -> bool:
    if MCP_SERVICE not in manifest.services:
        return False
    return not any(
        p.type is ParameterType.FILE and p.required for p in manifest.parameters
    )


async def catalogue(
    conn: asyncpg.Connection, principal: Principal
) -> dict[str, tuple[str, str, Manifest]]:
    """Tool name -> (repository, workspace, manifest), for this principal.

    Built per request rather than cached: publishing a workspace must show up
    immediately, and the scope filter is per-account so a cache would have to
    be keyed by account anyway.
    """
    rows = await conn.fetch(
        """
        SELECT r.name AS repository, w.name AS workspace, w.manifest
          FROM workspaces w
          JOIN repositories r ON r.id = w.repository_id
         ORDER BY r.name, w.name
        """
    )
    out: dict[str, tuple[str, str, Manifest]] = {}
    for row in rows:
        repo, ws = row["repository"], row["workspace"]
        if not principal.allows_repo(repo):
            continue
        manifest = Manifest.model_validate(json.loads(row["manifest"]))
        if not callable_by_mcp(manifest):
            continue
        name = tool_name(repo, ws)
        if name in out:
            # Two workspaces claiming one tool name would make whichever lost
            # unreachable, with nothing to show for it. Names come from an
            # administrator, so this is loud rather than silently resolved.
            clash = out[name]
            raise RpcError(
                INTERNAL_ERROR,
                f"tool name {name!r} is claimed by both "
                f"{clash[0]}/{clash[1]} and {repo}/{ws}",
            )
        out[name] = (repo, ws, manifest)
    return out


def _tool_json(name: str, repo: str, ws: str, manifest: Manifest) -> dict[str, Any]:
    return {
        "name": name,
        "title": f"{repo} / {ws}",
        "description": manifest.description or f"Run the {ws} workspace in {repo}.",
        "inputSchema": input_schema(manifest),
    }


# -- results ---------------------------------------------------------------


def _artifact_url(job_id: Any, filename: str) -> str:
    return (
        f"{config.PUBLIC_URL}/rest/v1/transformations/jobs/id/{job_id}"
        f"/artifacts/{filename}"
    )


def _content_blocks(row: asyncpg.Record) -> list[dict[str, Any]]:
    """Artifacts as MCP content. Text inline, everything else as a link.

    Binary is deliberately not base64'd into the response. An MCP client can
    fetch the URL with the same token it used to call the tool, so inlining a
    zip would spend context on bytes the model cannot read.
    """
    blocks: list[dict[str, Any]] = []
    for artifact in json.loads(row["artifacts"]):
        name, mime = artifact["name"], artifact.get("type", "")
        url = _artifact_url(row["id"], artifact["file"])
        if not (mime.startswith("text/") or mime in ("application/json",)):
            blocks.append({"type": "text", "text": f"[{name}] {mime} -- {url}"})
            continue
        path = execute.artifact_path(row["id"], artifact["file"])
        data = path.read_bytes()
        text = data[:MAX_INLINE_BYTES].decode("utf-8", "replace")
        if len(data) > MAX_INLINE_BYTES:
            text += f"\n\n[truncated at {MAX_INLINE_BYTES} bytes -- full output: {url}]"
        blocks.append({"type": "text", "text": text})

    if not blocks:
        blocks.append({"type": "text", "text": "The workspace produced no output."})
    return blocks


# -- methods ---------------------------------------------------------------


async def _initialize(_: Principal, params: dict[str, Any]) -> dict[str, Any]:
    # The client's requested protocolVersion is echoed only if we speak it;
    # otherwise we answer with ours and let the client decide, which is what
    # the transport asks for.
    requested = params.get("protocolVersion")
    return {
        "protocolVersion": requested if requested == PROTOCOL_VERSION else PROTOCOL_VERSION,
        "capabilities": {"tools": {"listChanged": False}},
        "serverInfo": SERVER_INFO,
    }


async def _tools_list(principal: Principal, _: dict[str, Any]) -> dict[str, Any]:
    async with db.pool().acquire() as conn:
        found = await catalogue(conn, principal)
    return {
        "tools": [
            _tool_json(name, repo, ws, manifest)
            for name, (repo, ws, manifest) in found.items()
        ]
    }


async def _tools_call(principal: Principal, params: dict[str, Any]) -> dict[str, Any]:
    name = params.get("name")
    if not isinstance(name, str):
        raise RpcError(INVALID_PARAMS, "name is required")
    arguments = params.get("arguments") or {}
    if not isinstance(arguments, dict):
        raise RpcError(INVALID_PARAMS, "arguments must be an object")

    async with db.pool().acquire() as conn:
        found = await catalogue(conn, principal)
    if name not in found:
        # Same answer whether the tool does not exist or this account cannot
        # see it: the catalogue is already scope-filtered, and distinguishing
        # the two would let a caller enumerate repositories it cannot reach.
        raise RpcError(INVALID_PARAMS, f"unknown tool {name!r}")

    repo, ws, _manifest = found[name]
    # jobs.submit validates and coerces against the manifest, so the values are
    # handed over as strings exactly as a query string would deliver them
    # rather than being coerced twice, in two places, differently.
    submitted = {k: _as_param(v) for k, v in arguments.items()}

    try:
        row, _ = await execute.run_sync(repo, ws, submitted, MCP_SERVICE)
    except ApiError as exc:
        # A workspace that fails is a *tool* error, not a protocol error: the
        # call was well-formed and the model is the one that needs to read the
        # failure and decide what to do. Returning a JSON-RPC error instead
        # would hide it from the model entirely.
        return {
            "content": [{"type": "text", "text": f"{exc.code}: {exc.message}"}],
            "isError": True,
        }

    return {"content": _content_blocks(row), "isError": False}


def _as_param(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float, str)):
        return str(value)
    return json.dumps(value)


async def _ping(_: Principal, __: dict[str, Any]) -> dict[str, Any]:
    return {}


METHODS = {
    "initialize": _initialize,
    "ping": _ping,
    "tools/list": _tools_list,
    "tools/call": _tools_call,
}


# -- transport -------------------------------------------------------------


def _rpc_error(request_id: Any, code: int, message: str, data: Any = None) -> JSONResponse:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return JSONResponse(
        content={"jsonrpc": "2.0", "id": request_id, "error": error},
        headers={"MCP-Protocol-Version": PROTOCOL_VERSION},
    )


@router.post("/mcp")
async def endpoint(request: Request) -> Response:
    principal = await auth.require_auth(request)

    try:
        body = json.loads(await request.body())
    except (json.JSONDecodeError, UnicodeDecodeError):
        return _rpc_error(None, PARSE_ERROR, "request body is not valid JSON")

    if isinstance(body, list):
        # Batching was removed in 2025-06-18. Accepting it anyway would mean
        # advertising a protocol version whose rules we do not follow.
        return _rpc_error(
            None, INVALID_REQUEST, "JSON-RPC batching is not supported"
        )
    if not isinstance(body, dict) or body.get("jsonrpc") != "2.0":
        return _rpc_error(None, INVALID_REQUEST, "not a JSON-RPC 2.0 message")

    method = body.get("method")
    request_id = body.get("id")
    params = body.get("params") or {}
    if not isinstance(params, dict):
        return _rpc_error(request_id, INVALID_PARAMS, "params must be an object")

    if request_id is None:
        # A notification. Nothing to answer, and answering anyway would be a
        # protocol violation rather than a harmless extra.
        return Response(
            status_code=202, headers={"MCP-Protocol-Version": PROTOCOL_VERSION}
        )

    handler = METHODS.get(method)
    if handler is None:
        return _rpc_error(request_id, METHOD_NOT_FOUND, f"unknown method {method!r}")

    try:
        result = await handler(principal, params)
    except RpcError as exc:
        return _rpc_error(request_id, exc.code, exc.message, exc.data)

    return JSONResponse(
        content={"jsonrpc": "2.0", "id": request_id, "result": result},
        headers={"MCP-Protocol-Version": PROTOCOL_VERSION},
    )


@router.get("/mcp")
async def no_server_stream() -> Response:
    return Response(status_code=405, headers={"Allow": "POST"})
