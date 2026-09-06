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
import time
from typing import Any

import asyncpg
from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse

from datum_sync import audit, auth, config, db, execute, proxy, vault_fs
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


# -- governance path classification ----------------------------------------
#
# Writes to these paths are flagged is_governance=true in mcp_call_log.
# The classification is server-side (post-normalisation) so a client cannot
# dodge it by encoding tricks. This constant should itself eventually be
# vault-resident so changes to it are observable, but a hardcoded set is the
# right starting point.

_GOVERNANCE_PATHS = frozenset({"SOUL.md"})
_GOVERNANCE_PREFIXES = ("skills/", "hooks/")


def _is_governance(target: str | None) -> bool:
    """Return True if target is a governance-class vault path."""
    if not target:
        return False
    return target in _GOVERNANCE_PATHS or any(
        target.startswith(p) for p in _GOVERNANCE_PREFIXES
    )


def _call_target(method: str, tool_name: str | None, params: dict[str, Any]) -> str | None:
    """Extract a safe, loggable target string from a tools/call invocation.

    Never returns content — only identifiers (paths, connection names).
    Returns None for methods with no meaningful target.
    """
    if method != "tools/call" or tool_name is None:
        return None
    args = params.get("arguments") or {}
    if tool_name in _VAULT_TOOL_NAMES:
        return args.get("path")
    if tool_name == "proxy_request":
        conn = args.get("connection", "")
        meth = args.get("method", "")
        path = args.get("path", "")
        return f"{conn}:{meth}:{path}" if conn else None
    # Workspace tools — log the tool name as target (the workspace identity).
    return tool_name


def _target_kind(method: str, tool_name: str | None) -> str | None:
    """What sort of thing `_call_target` just returned.

    Branches deliberately in lockstep with `_call_target` above, over the same
    constants. They are two functions rather than one returning a pair because
    `_call_target` is a tested, guard-anchored signature and splitting its
    return type to add a label is a bigger change than repeating four lines.
    `test_target_kind_agrees_with_call_target` is what stops them drifting.
    """
    if method != "tools/call" or tool_name is None:
        return None
    if tool_name in _VAULT_TOOL_NAMES:
        return "vault_path"
    if tool_name == "proxy_request":
        return "connection"
    return "tool"


def _verb(method: str) -> str:
    """The audit verb for a JSON-RPC method: `mcp.tools.call`, `mcp.ping`, ...

    Vocabulary in spec/datum-gate/14-audit.md §3. Derived rather than looked
    up in a table, so a method added to METHODS cannot start writing rows with
    no verb -- a NOT NULL column would drop the row, and dropping it is the
    one failure a log cannot report.
    """
    return "mcp." + method.replace("/", ".")


async def _log_call(
    principal: Principal,
    method: str,
    tool_name_val: str | None,
    target: str | None,
    outcome: str,
    error_code: int | None,
    duration_ms: int,
    trace: audit.Trace,
) -> None:
    """Write one row to mcp_call_log and one to audit_log. Never raises.

    Both, not one. `mcp_call_log` keeps its exact existing behaviour so that
    the two tables should agree row for row -- and that agreement is how the
    new table gets checked before anything is retired. In particular the
    `outcome` written here is the old classification, warts and all: a tool
    call refused by an access check returns an `isError` result rather than
    raising RpcError, so it is recorded as 'ok' in *both* tables. Correcting
    that is a separate change; doing it here would mean any disagreement
    between the tables had two possible causes instead of one.

    One connection, two inserts. Acquiring twice would double this path's
    hold on the pool for no gain.
    """
    try:
        async with db.pool().acquire() as conn:
            await conn.execute(
                """
                INSERT INTO mcp_call_log
                    (account_id, account_name, method, tool_name, target,
                     is_governance, outcome, error_code, duration_ms, client_trace_id)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                """,
                principal.account_id,
                principal.name,
                method,
                tool_name_val,
                target,
                _is_governance(target),
                outcome,
                error_code,
                duration_ms,
                trace.client_id,
            )
            await audit.write(
                conn,
                trace=trace,
                principal=principal,
                via="mcp",
                verb=_verb(method),
                target_kind=_target_kind(method, tool_name_val),
                target=target,
                outcome=outcome,
                error_code=error_code,
                duration_ms=duration_ms,
                governance=_is_governance(target),
                detail={"tool": tool_name_val} if tool_name_val else None,
            )
    except Exception:
        pass  # logging failure must never surface to the caller


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


async def _initialize(
    _: Principal, params: dict[str, Any], __: audit.Trace
) -> dict[str, Any]:
    # The client's requested protocolVersion is echoed only if we speak it;
    # otherwise we answer with ours and let the client decide, which is what
    # the transport asks for.
    requested = params.get("protocolVersion")
    return {
        "protocolVersion": requested if requested == PROTOCOL_VERSION else PROTOCOL_VERSION,
        "capabilities": {"tools": {"listChanged": False}},
        "serverInfo": SERVER_INFO,
    }


PROXY_TOOL = {
    "name": "proxy_request",
    "title": "Credential Proxy",
    "description": (
        "Forward an HTTP request through a named connection. "
        "The connection's credentials are injected server-side; "
        "the caller never sees the key."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "connection": {
                "type": "string",
                "description": "Connection name from the connection store",
            },
            "method": {
                "type": "string",
                "enum": ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"],
            },
            "path": {
                "type": "string",
                "description": "Path appended to the connection's base_url",
            },
            "headers": {
                "type": "object",
                "description": "Additional request headers",
            },
            "body": {
                "description": "Request body (JSON-serializable)",
            },
            "query_params": {
                "type": "object",
                "description": "URL query parameters",
            },
        },
        "required": ["connection", "method", "path"],
    },
}


VAULT_READ_TOOL = {
    "name": "vault_read",
    "title": "Vault Read",
    "description": (
        "Read a file from the vault. The path is relative to the vault root "
        "and must be within the caller's read scope."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Vault-relative path, e.g. dev/config/foo.md",
            },
        },
        "required": ["path"],
    },
}

VAULT_WRITE_TOOL = {
    "name": "vault_write",
    "title": "Vault Write",
    "description": (
        "Write a file to the vault. Creates parent directories as needed. "
        "The path must be within the caller's write scope."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Vault-relative path, e.g. dev/notes/idea.md",
            },
            "content": {
                "type": "string",
                "description": "File content (UTF-8 text)",
            },
        },
        "required": ["path", "content"],
    },
}

VAULT_LIST_TOOL = {
    "name": "vault_list",
    "title": "Vault List",
    "description": (
        "List a directory in the vault. Only entries within the caller's "
        "read scope are shown."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Vault-relative directory path, e.g. dev/config",
            },
        },
        "required": ["path"],
    },
}

VAULT_TOOLS = [VAULT_READ_TOOL, VAULT_WRITE_TOOL, VAULT_LIST_TOOL]


async def _tools_list(
    principal: Principal, _: dict[str, Any], __: audit.Trace
) -> dict[str, Any]:
    async with db.pool().acquire() as conn:
        found = await catalogue(conn, principal)
    tools = [
        _tool_json(name, repo, ws, manifest)
        for name, (repo, ws, manifest) in found.items()
    ]
    tools.append(PROXY_TOOL)
    # Vault tools are visible only to callers with a vault scope. An account
    # with no scope would see tools that always return 403 — fewer tools is
    # better than broken ones.
    if principal.vault_scope:
        tools.extend(VAULT_TOOLS)
    return {"tools": tools}


async def _proxy_call(
    principal: Principal,
    args: dict[str, Any],
    trace: audit.Trace,
) -> dict[str, Any]:
    """Dispatch a proxy_request tool call."""
    connection = args.get("connection")
    if not isinstance(connection, str):
        raise RpcError(INVALID_PARAMS, "connection is required")
    method = args.get("method")
    if not isinstance(method, str):
        raise RpcError(INVALID_PARAMS, "method is required")
    path = args.get("path", "")
    try:
        return await proxy.proxy_request(
            principal=principal,
            connection_name=connection,
            method=method,
            path=path,
            trace=trace,
            headers=args.get("headers"),
            body=args.get("body"),
            query_params=args.get("query_params"),
        )
    except ApiError as exc:
        return {
            "content": [{"type": "text", "text": f"{exc.code}: {exc.message}"}],
            "isError": True,
        }


async def _vault_call(principal: Principal, name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Dispatch a vault_* tool call."""
    path = args.get("path")
    if not isinstance(path, str):
        raise RpcError(INVALID_PARAMS, "path is required")
    try:
        if name == "vault_read":
            return await vault_fs.read(principal, path)
        elif name == "vault_write":
            content = args.get("content")
            if not isinstance(content, str):
                raise RpcError(INVALID_PARAMS, "content is required")
            return await vault_fs.write(principal, path, content)
        elif name == "vault_list":
            return await vault_fs.list_dir(principal, path)
    except ApiError as exc:
        return {
            "content": [{"type": "text", "text": f"{exc.code}: {exc.message}"}],
            "isError": True,
        }
    raise RpcError(INVALID_PARAMS, f"unknown vault tool {name!r}")


_VAULT_TOOL_NAMES = frozenset({"vault_read", "vault_write", "vault_list"})


async def _tools_call(
    principal: Principal, params: dict[str, Any], trace: audit.Trace
) -> dict[str, Any]:
    name = params.get("name")
    if not isinstance(name, str):
        raise RpcError(INVALID_PARAMS, "name is required")
    arguments = params.get("arguments") or {}
    if not isinstance(arguments, dict):
        raise RpcError(INVALID_PARAMS, "arguments must be an object")

    # Static tools — not workspace-derived.
    if name == "proxy_request":
        return await _proxy_call(principal, arguments, trace)
    if name in _VAULT_TOOL_NAMES:
        return await _vault_call(principal, name, arguments)

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


async def _ping(
    _: Principal, __: dict[str, Any], ___: audit.Trace
) -> dict[str, Any]:
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
    # Minted once per inbound request by the `trace_and_audit` middleware, and
    # passed down from here. Read rather than minted so that one request has one
    # trace: this endpoint used to mint its own, which was correct while it was
    # the only writer, but became a second trace for the same request the moment
    # anything upstream had one. No fallback if the attribute is missing --
    # minting one here would paper over the middleware not running, and the
    # symptom would be rows that quietly fail to join.
    trace: audit.Trace = request.state.trace

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

    tool_name_val = params.get("name") if method == "tools/call" else None
    target = _call_target(method, tool_name_val, params)
    t0 = time.monotonic()

    try:
        result = await handler(principal, params, trace)
    except RpcError as exc:
        duration_ms = int((time.monotonic() - t0) * 1000)
        await _log_call(
            principal, method, tool_name_val, target,
            "error", exc.code, duration_ms, trace,
        )
        return _rpc_error(request_id, exc.code, exc.message, exc.data)

    duration_ms = int((time.monotonic() - t0) * 1000)
    await _log_call(
        principal, method, tool_name_val, target,
        "ok", None, duration_ms, trace,
    )
    return JSONResponse(
        content={"jsonrpc": "2.0", "id": request_id, "result": result},
        headers={"MCP-Protocol-Version": PROTOCOL_VERSION},
    )


@router.get("/mcp")
async def no_server_stream() -> Response:
    return Response(status_code=405, headers={"Allow": "POST"})
