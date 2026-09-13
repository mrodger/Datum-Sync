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
import uuid
from typing import Any

import asyncpg
from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse

import dataclasses
import hashlib

from datum_sync import audit, auth, config, db, execute, pending, proxy, sessions, vault_fs
from datum_sync import jobs as jobs_mod
from datum_sync.federation import catalogue as fedcat, client as fedclient, guards as guards_mod
from datum_sync.auth import Principal
from datum_sync.errors import ApiError
from datum_sync.manifest import Manifest, ParameterType

router = APIRouter()

PROTOCOL_VERSION = "2025-06-18"

SERVER_INFO = {"name": "datum-sync", "version": "0.1.0"}

# The service a workspace must publish to be reachable as a tool.
MCP_SERVICE = "data_streaming"

# Calling a workspace tool submits a job, which is a tier-3 verb.
WORKSPACE_MIN_TIER = 3

# Beyond this an artifact is linked rather than inlined. A multi-megabyte
# result pasted into a conversation is not usable by the model and displaces
# the context it needs to interpret it.
MAX_INLINE_BYTES = 64 * 1024

_UNSAFE_TOOL_CHAR = re.compile(r"[^A-Za-z0-9_-]")

# Tool names are capped so that a client prefix (`mcp__datum-sync__`, 17
# characters, is what the Datum-3.0 bridge adds; Claude Code drops any tool
# over 64) still fits. A name over the cap keeps its first characters and
# ends in a hash of the whole, so two long names stay distinct and a given
# workspace always gets the same name (spec 12 §1, MCP-020).
MAX_TOOL_NAME = 48

# JSON-RPC 2.0 error codes.
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
# Server-defined (spec 03 §5): the principal's concurrent-session limit.
SESSION_LIMIT = -32000


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
    session_id: str | None = None,
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
                # The *account*: for an agent, its sponsor. This table predates
                # agents being rows and has no agent column; audit_log carries
                # the actor. Keeping the old meaning is what lets the two be
                # compared before this one is retired (WP7).
                principal.parent_id if principal.kind == "agent" else principal.account_id,
                principal.parent_name if principal.kind == "agent" else principal.name,
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
                session_id=session_id,
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
    return cap_tool_name(_UNSAFE_TOOL_CHAR.sub("_", f"{repo}__{ws}"))


def cap_tool_name(name: str) -> str:
    if len(name) <= MAX_TOOL_NAME:
        return name
    digest = hashlib.sha1(name.encode()).hexdigest()[:6]
    return f"{name[:MAX_TOOL_NAME - 7]}_{digest}"


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
        "capabilities": {"tools": {"listChanged": False}, "resources": {"listChanged": False}},
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

# Built-in tools that every principal gets, by tier (spec 07 §6). Shown only
# when usable, like the vault tools: a tool that can only refuse is noise.
WHOAMI_TOOL = {
    "name": "whoami",
    "title": "Who am I",
    "description": "This credential's principal, effective tier, limits, scopes, and the "
                   "OAuth scope that would unlock more.",
    "inputSchema": {"type": "object", "properties": {}},
    "annotations": {"readOnlyHint": True, "datumMinTier": 1},
}
SESSION_INFO_TOOL = {
    "name": "session_info",
    "title": "Session",
    "description": "This MCP session, the principal's session limit, and the other live "
                   "sessions (ids and ages only).",
    "inputSchema": {"type": "object", "properties": {}},
    "annotations": {"readOnlyHint": True, "datumMinTier": 1},
}
JOB_STATUS_TOOL = {
    "name": "job_status",
    "title": "Job status",
    "description": "Status, progress and the last log lines of a job you submitted "
                   "(or, at tier 4, any job in scope).",
    "inputSchema": {"type": "object", "properties": {"job_id": {"type": "string"}},
                    "required": ["job_id"]},
    "annotations": {"readOnlyHint": True, "datumMinTier": 2},
}
JOB_RESULT_TOOL = {
    "name": "job_result",
    "title": "Job result",
    "description": "The output of a completed job, as the tool call would have returned it; "
                   "or its handle again if it is still running.",
    "inputSchema": {"type": "object", "properties": {"job_id": {"type": "string"}},
                    "required": ["job_id"]},
    "annotations": {"readOnlyHint": True, "datumMinTier": 2},
}
JOB_LIST_TOOL = {
    "name": "job_list",
    "title": "My jobs",
    "description": "Your most recent jobs.",
    "inputSchema": {"type": "object", "properties": {
        "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10}}},
    "annotations": {"readOnlyHint": True, "datumMinTier": 2},
}
JOB_CANCEL_TOOL = {
    "name": "job_cancel",
    "title": "Cancel job",
    "description": "Cancel a queued or running job you submitted.",
    "inputSchema": {"type": "object", "properties": {"job_id": {"type": "string"}},
                    "required": ["job_id"]},
    "annotations": {"datumMinTier": 3},
}
ELEVATE_TOOL = {
    "name": "elevate",
    "title": "Elevate",
    "description": "Ask for a wider OAuth scope (mcp, mcp:operate, mcp:admin) through the "
                   "device flow. Returns a user code for a person to approve on the "
                   "Approvals screen and a device code to poll /oauth/token with "
                   "(grant_type urn:ietf:params:oauth:grant-type:device_code, "
                   "client_id datum-sync-elevate).",
    "inputSchema": {"type": "object", "properties": {
        "scope": {"type": "string", "enum": ["mcp", "mcp:operate", "mcp:admin"]}},
        "required": ["scope"]},
    "annotations": {"datumMinTier": 1},
}
PENDING_STATUS_TOOL = {
    "name": "pending_status",
    "title": "Pending call status",
    "description": "Whether a federated call you made that was held for approval has been decided.",
    "inputSchema": {"type": "object", "properties": {"pending_id": {"type": "integer"}},
                    "required": ["pending_id"]},
    "annotations": {"readOnlyHint": True, "datumMinTier": 1},
}
PENDING_RESULT_TOOL = {
    "name": "pending_result",
    "title": "Pending call result",
    "description": "The result of an approved and executed call you made; access_denied if it was rejected.",
    "inputSchema": {"type": "object", "properties": {"pending_id": {"type": "integer"}},
                    "required": ["pending_id"]},
    "annotations": {"readOnlyHint": True, "datumMinTier": 1},
}
_BUILTIN_TOOL_NAMES = frozenset({
    "whoami", "session_info", "job_status", "job_result", "job_list", "job_cancel", "elevate",
    "pending_status", "pending_result",
})


async def _tools_list(
    principal: Principal, _: dict[str, Any], __: audit.Trace
) -> dict[str, Any]:
    tier = principal.effective_tier
    tools: list[dict[str, Any]] = [WHOAMI_TOOL, SESSION_INFO_TOOL]
    # Shown only when there is something to unlock.
    if auth.elevation_hints(principal):
        tools.append(ELEVATE_TOOL)
    if tier >= 2:
        tools += [JOB_STATUS_TOOL, JOB_RESULT_TOOL, JOB_LIST_TOOL]
    if tier >= 3:
        tools.append(JOB_CANCEL_TOOL)
    # Calling a workspace tool submits a job, a tier-3 verb (spec 03 §6). A
    # caller below that would see tools that always answer TIER_REQUIRED --
    # fewer tools is better than broken ones, the same rule the vault tools
    # follow below. The catalogue is not even built for them: the query is
    # the cost, and the answer is known.
    if tier >= WORKSPACE_MIN_TIER:
        async with db.pool().acquire() as conn:
            found = await catalogue(conn, principal)
        tools = [
            _tool_json(name, repo, ws, manifest)
            for name, (repo, ws, manifest) in found.items()
        ]
    # Federated tools (spec 05 §3.2): from the cache, never an upstream, for
    # the connections the principal's federation blocks name. Local names
    # win a clash, so the catalogue's names are excluded (FED-019).
    if principal.federation_scope:
        async with db.pool().acquire() as conn:
            tools += await fedcat.visible_tools(conn, principal, exclude={t["name"] for t in tools})
        tools += [PENDING_STATUS_TOOL, PENDING_RESULT_TOOL]
    # The proxy needs tier 3 and a grant; without either it can only refuse.
    if tier >= 3 and principal.proxy_grants:
        tools.append(PROXY_TOOL)
    # Vault tools are visible only to callers with a vault scope. An account
    # with no scope would see tools that always return 403. Reads are tier 1;
    # the write tool is tier 3 and hidden below it.
    if principal.vault_scope:
        tools.extend([VAULT_READ_TOOL, VAULT_LIST_TOOL])
        if tier >= 3:
            tools.append(VAULT_WRITE_TOOL)
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
            # Writing is tier 3 (spec 03 §6). Checked before the scope so a
            # baseline token is told to elevate, not that its path is wrong.
            auth.require_tier(principal, 3, "vault_write")
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
    if name in _BUILTIN_TOOL_NAMES:
        return await _builtin_call(principal, name, arguments, trace)

    async with db.pool().acquire() as conn:
        found = await catalogue(conn, principal)
        fed = None if name in found else await fedcat.lookup(conn, name)
    if name not in found and fed is not None:
        return await _federated_call(principal, fed[0], fed[1], arguments, trace)
    if name not in found:
        # Same answer whether the tool does not exist or this account cannot
        # see it: the catalogue is already scope-filtered, and distinguishing
        # the two would let a caller enumerate repositories it cannot reach.
        raise RpcError(INVALID_PARAMS, f"unknown tool {name!r}")

    repo, ws, _manifest = found[name]
    # jobs.submit validates and coerces against the manifest, so the values are
    # handed over as strings exactly as a query string would deliver them
    # rather than being coerced twice, in two places, differently.
    # `_wait_seconds` is the caller's, not the workspace's: how long this
    # call may hold the connection before returning a job handle (07 §6).
    wait = arguments.pop("_wait_seconds", None)
    try:
        wait = float(wait) if wait is not None else float(config.MCP_WAIT_SECONDS)
    except (TypeError, ValueError):
        raise RpcError(INVALID_PARAMS, "_wait_seconds must be a number")
    wait = max(0.0, min(wait, 300.0))
    submitted = {k: _as_param(v) for k, v in arguments.items()}

    try:
        # `submitted_by` is the job row's own record of who asked. The audit
        # row written by `_log_call` already names the principal, but the Jobs
        # screen and the automations engine read `jobs.submitted_by`, and for
        # every MCP-submitted job it was NULL until this argument was passed.
        row, _ = await execute.run_sync(
            repo, ws, submitted, MCP_SERVICE, submitted_by=principal.name,
            principal=principal, wait_seconds=wait,
            session_id=trace.session_id, trace_id=trace.id,
        )
    except ApiError as exc:
        if exc.code == "TIMEOUT" and exc.detail.get("job_id"):
            # Still running: a handle, not an error. The client polls with
            # job_status and fetches with job_result.
            async with db.pool().acquire() as conn:
                return await _job_handle(conn, principal, exc.detail["job_id"])
        # A workspace that fails is a *tool* error, not a protocol error: the
        # call was well-formed and the model is the one that needs to read the
        # failure and decide what to do. Returning a JSON-RPC error instead
        # would hide it from the model entirely.
        return {
            "content": [{"type": "text", "text": f"{exc.code}: {exc.message}"}],
            "isError": True,
        }

    return {"content": _content_blocks(row), "isError": False}


# -- federation (spec 05 §4.2) ------------------------------------------------------

_drive_cache: dict[tuple[str, str], tuple[float, list[str]]] = {}


def _tool_error(code: str, message: str, **extra: Any) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": f"{code}: {message}"}],
            "isError": True, "structuredContent": {"code": code, **extra}}


async def _drive_resolver(upstream: fedclient.Upstream, connection: str):
    """`resolve: drive_folder`: a file id to its ancestor folder ids, cached."""
    async def resolve(file_id: str) -> list[str]:
        key = (connection, file_id)
        hit = _drive_cache.get(key)
        if hit and time.time() - hit[0] < config.DRIVE_RESOLVE_TTL_SECONDS:
            return hit[1]
        chain: list[str] = []
        current = file_id
        for _ in range(20):
            result = await upstream.tools_call("get_file", {"fileId": current})
            if result.get("isError"):
                break  # the top of the tree, or a folder this credential cannot read
            body = result.get("structuredContent")
            if not isinstance(body, dict):
                text = next((b.get("text") for b in result.get("content") or [] if b.get("type") == "text"), "")
                try:
                    body = json.loads(text) if text else {}
                except json.JSONDecodeError:
                    break
            parents = body.get("parents") or []
            if not parents:
                break
            chain.append(str(parents[0]))
            current = str(parents[0])
        if not chain:
            raise ValueError("no parent folder")
        _drive_cache[key] = (time.time(), chain)
        return chain
    return resolve


async def _federated_call(
    principal: Principal, tool: asyncpg.Record, connection: asyncpg.Record,
    args: dict[str, Any], trace: audit.Trace,
) -> dict[str, Any]:
    found = fedcat.block_for(principal, connection)
    if found is None:
        # Not in the principal's block, or under the connection's tier when
        # the block exists: the same answer as no such tool (never reveal).
        if principal.federation_scope and principal.effective_tier < connection["tier"]:
            return _tool_error("TIER_REQUIRED", f"{connection['name']} is tier {connection['tier']}",
                               required=connection["tier"], effective=principal.effective_tier,
                               elevate=auth.elevation_hints(principal))
        raise RpcError(INVALID_PARAMS, f"unknown tool {tool['tool_name']!r}")
    _, block = found
    cfg = json.loads(connection["config"])
    upstream_name = tool["upstream_name"]
    if not guards_mod.tool_allowed(block, upstream_name):
        raise RpcError(INVALID_PARAMS, f"unknown tool {tool['tool_name']!r}")
    if len(json.dumps(args)) > guards_mod.MAX_ARGS_BYTES:
        # Refused before evaluation (FED-020): a guard walks these.
        return _tool_error("ARGUMENTS_TOO_LARGE", f"arguments over {guards_mod.MAX_ARGS_BYTES} bytes")
    compiled = guards_mod.compile_guards(cfg.get("guards"))
    matching = [g for g in compiled if g.matches(upstream_name)]
    if not matching and cfg.get("default", "deny") != "allow":
        raise RpcError(INVALID_PARAMS, f"unknown tool {tool['tool_name']!r}")
    target = f"{connection['name']}:{upstream_name}"

    async with db.pool().acquire() as conn:
        upstream = await fedcat.upstream_for(conn, connection)
        try:
            guarded, approvals = await guards_mod.evaluate(
                matching, block, principal.effective_tier, args,
                resolver=await _drive_resolver(upstream, connection["name"]),
            )
        except guards_mod.Denied as exc:
            await audit.write(
                conn, trace=trace, principal=principal, via="mcp", verb="federate.denied",
                target_kind="tool", target=target, outcome="denied", session_id=trace.session_id,
                detail={"guard": exc.guard, "field": exc.field},
            )
            return _tool_error("FEDERATION_DENIED", str(exc), guard=exc.guard, field=exc.field)
        held = [a for a in approvals if a in (block.get("approval_required") or [])]
        if held:
            # Parked for a person (spec 05 §5): the guards passed, the label
            # is one the block says needs approval, nothing is forwarded.
            parked = await pending.request(
                conn, principal, connection["name"], upstream_name, args, held[0], trace,
            )
            return pending.handle(parked)
        # Audited BEFORE forwarding, with the guarded values only (FED-013):
        # the arguments themselves may be a file body or a command.
        await audit.write(
            conn, trace=trace, principal=principal, via="mcp", verb="federate.call",
            target_kind="tool", target=target, outcome="ok", session_id=trace.session_id,
            detail=guarded or {"guard": "none"},
        )
    t0 = time.monotonic()
    try:
        result = await upstream.tools_call(upstream_name, args)
    except fedclient.UpstreamError as exc:
        # The upstream is down or broken: a tool error, fast, naming the
        # outage (FED-012). The listing stays served from the cache.
        result = _tool_error(exc.code, exc.message)
    except fedclient.ToolError as exc:
        result = _tool_error("UPSTREAM_ERROR", f"{exc.code}: {exc.message}")
    async with db.pool().acquire() as conn:
        await audit.write(
            conn, trace=trace, principal=principal, via="mcp", verb="federate.result",
            target_kind="tool", target=target, outcome="error" if result.get("isError") else "ok",
            duration_ms=int((time.monotonic() - t0) * 1000), session_id=trace.session_id,
        )
    return result


async def execute_approved(
    conn: asyncpg.Connection, requester: Principal, connection_name: str, upstream_name: str,
    args: dict[str, Any], trace: audit.Trace,
) -> dict[str, Any]:
    """Forward an approved pending call under the requester's snapshot.

    The guards run again against the snapshot's block -- the approver saw
    the call as it was -- with the approval requirement now satisfied.
    """
    connection = await conn.fetchrow(
        f"SELECT {fedcat.connections._COLUMNS} FROM connections WHERE name = $1 AND type = 'mcp'", connection_name
    )
    if connection is None:
        return _tool_error("UPSTREAM_UNAVAILABLE", f"connection {connection_name!r} is gone")
    found = fedcat.block_for(requester, connection)
    if found is None:
        return _tool_error("FEDERATION_DENIED", "the snapshot no longer covers the connection")
    _, block = found
    cfg = json.loads(connection["config"])
    compiled = guards_mod.compile_guards(cfg.get("guards"))
    matching = [g for g in compiled if g.matches(upstream_name)]
    upstream = await fedcat.upstream_for(conn, connection)
    try:
        guarded, _ = await guards_mod.evaluate(
            matching, block, requester.effective_tier, args,
            resolver=await _drive_resolver(upstream, connection_name),
        )
    except guards_mod.Denied as exc:
        return _tool_error("FEDERATION_DENIED", str(exc), guard=exc.guard, field=exc.field)
    target = f"{connection_name}:{upstream_name}"
    await audit.write(
        conn, trace=trace, principal=requester, via="mcp", verb="federate.call", target_kind="tool",
        target=target, outcome="ok", detail={**(guarded or {"guard": "none"}), "approved": True},
    )
    try:
        return await upstream.tools_call(upstream_name, args)
    except fedclient.UpstreamError as exc:  # approved: same answer, no session
        return _tool_error(exc.code, exc.message)
    except fedclient.ToolError as exc:
        return _tool_error("UPSTREAM_ERROR", f"{exc.code}: {exc.message}")


async def _resources_list(principal: Principal, _: dict[str, Any], __: audit.Trace) -> dict[str, Any]:
    if not principal.federation_scope:
        return {"resources": []}
    async with db.pool().acquire() as conn:
        return {"resources": await fedcat.visible_resources(conn, principal, templates=False)}


async def _resources_templates_list(principal: Principal, _: dict[str, Any], __: audit.Trace) -> dict[str, Any]:
    if not principal.federation_scope:
        return {"resourceTemplates": []}
    async with db.pool().acquire() as conn:
        return {"resourceTemplates": await fedcat.visible_resources(conn, principal, templates=True)}


async def _resources_read(principal: Principal, params: dict[str, Any], trace: audit.Trace) -> dict[str, Any]:
    """`datum://{connection}/{upstream uri}`, guarded by the connection's
    resource_guards against the principal's block (spec 05 §4.5, FED-016)."""
    uri = params.get("uri")
    if not isinstance(uri, str) or not uri.startswith("datum://"):
        raise RpcError(INVALID_PARAMS, "uri is datum://{connection}/{uri}")
    name, _, upstream_uri = uri[len("datum://"):].partition("/")
    async with db.pool().acquire() as conn:
        connection = await conn.fetchrow(
            f"SELECT {fedcat.connections._COLUMNS} FROM connections WHERE name = $1 AND type = 'mcp'", name
        )
        found = fedcat.block_for(principal, connection) if connection else None
        if found is None:
            raise RpcError(INVALID_PARAMS, f"unknown resource {uri!r}")
        _, block = found
        cfg = json.loads(connection["config"])
        rguards = guards_mod.compile_resource_guards(cfg.get("resource_guards"))
        picked = guards_mod.resource_value(rguards, upstream_uri)
        target = f"{name}:{upstream_uri}"
        denied = None
        if picked is None:
            if cfg.get("default", "deny") != "allow":
                denied = "no resource guard covers this uri"
        else:
            field, value = picked
            if field == "commands" or not any(
                fedcat.guards_mod.grants.matches(p, value) for p in guards_mod._field(block, field)
            ):
                denied = f"{value!r} is outside {field}"
        if denied:
            await audit.write(
                conn, trace=trace, principal=principal, via="mcp", verb="federate.denied",
                target_kind="resource", target=target, outcome="denied", session_id=trace.session_id,
                detail={"field": picked[0] if picked else None},
            )
            raise RpcError(INVALID_PARAMS, f"resource denied: {denied}")
        await audit.write(
            conn, trace=trace, principal=principal, via="mcp", verb="federate.call",
            target_kind="resource", target=target, outcome="ok", session_id=trace.session_id,
            detail={picked[0]: [picked[1]]} if picked else {"guard": "none"},
        )
        upstream = await fedcat.upstream_for(conn, connection)
    try:
        result = await upstream.resources_read(upstream_uri)
    except (fedclient.UpstreamError, fedclient.ToolError) as exc:
        raise RpcError(INTERNAL_ERROR, f"upstream: {exc}")
    contents = result.get("contents") or result.get("content") or []
    for block_ in contents:
        if isinstance(block_, dict) and isinstance(block_.get("uri"), str):
            block_["uri"] = f"datum://{name}/{block_['uri']}"
    return {"contents": contents}


async def _job_visible(conn: asyncpg.Connection, principal: Principal, raw_id: str) -> asyncpg.Record:
    """A job the caller may see: its own, its child's, or (tier 4) any in scope."""
    try:
        job_id = uuid.UUID(str(raw_id))
    except ValueError:
        raise ApiError(400, "INVALID_PARAMETER", f"not a job id: {raw_id!r}")
    row = await jobs_mod.get(conn, job_id)
    if row is None:
        raise ApiError(404, "NOT_FOUND", f"no such job {raw_id}")
    if not principal.allows_repo(row["repository"]):
        raise ApiError(404, "NOT_FOUND", f"no such job {raw_id}")
    snapshot = auth.json_of(row, "grant_snapshot") or {}
    mine = row["submitted_by"] == principal.name
    child = snapshot.get("parent_id") == principal.account_id
    if not (mine or child or principal.effective_tier >= 4):
        raise ApiError(404, "NOT_FOUND", f"no such job {raw_id}")
    return row


async def _job_handle(conn: asyncpg.Connection, principal: Principal, raw_id: str) -> dict[str, Any]:
    row = await _job_visible(conn, principal, raw_id)
    started = row["started_at"]
    elapsed = int((time.time() - started.timestamp())) if started else 0
    tail = await conn.fetch(
        "SELECT level, message FROM job_log WHERE job_id = $1 ORDER BY id DESC LIMIT 5",
        row["id"],
    )
    lines = [f"{r['level']}: {r['message']}" for r in reversed(tail)]
    text = (
        f"Job {row['id']} is {row['status']}"
        + (f" (started {elapsed}s ago)." if started else ".")
        + "\nCall job_status with this job_id to check, or job_result to fetch the "
        "output when it completes."
        + ("\n\nRecent log:\n" + "\n".join(lines) if lines else "")
    )
    return {
        "content": [{"type": "text", "text": text}],
        "structuredContent": {
            "job_id": str(row["id"]), "status": row["status"],
            "submitted_at": row["submitted_at"].isoformat(),
            "started_at": started.isoformat() if started else None,
        },
        "isError": False,
    }


async def _builtin_call(
    principal: Principal, name: str, args: dict[str, Any], trace: audit.Trace
) -> dict[str, Any]:
    try:
        if name == "whoami":
            body = auth.principal_json(principal)
            if trace.session_id:
                body["session"] = {"id": trace.session_id}
            return {"content": [{"type": "text", "text": json.dumps(body, indent=1)}],
                    "structuredContent": body, "isError": False}
        if name == "session_info":
            async with db.pool().acquire() as conn:
                rows = await sessions.live(conn, principal)
            body = {
                "session_id": trace.session_id,
                "limit": sessions.limit_for(principal),
                "idle_seconds": sessions.idle_window(principal),
                "live": [{"id": str(r["id"]), "started_at": r["started_at"].isoformat(),
                          "idle_seconds": int(r["idle_seconds"]),
                          "client_name": r["client_name"], "this": str(r["id"]) == trace.session_id}
                         for r in rows],
            }
            return {"content": [{"type": "text", "text": json.dumps(body, indent=1)}],
                    "structuredContent": body, "isError": False}
        if name in ("job_status", "job_result", "job_cancel"):
            auth.require_tier(principal, 3 if name == "job_cancel" else 2, name)
            raw_id = args.get("job_id")
            if not isinstance(raw_id, str):
                raise RpcError(INVALID_PARAMS, "job_id is required")
            async with db.pool().acquire() as conn:
                if name == "job_cancel":
                    await _job_visible(conn, principal, raw_id)
                    result = await jobs_mod.cancel(conn, uuid.UUID(raw_id))
                    return {"content": [{"type": "text", "text": f"job {raw_id}: {result}"}],
                            "structuredContent": {"job_id": raw_id, "result": result},
                            "isError": False}
                row = await _job_visible(conn, principal, raw_id)
                if name == "job_result" and row["status"] == "complete":
                    return {"content": _content_blocks(row),
                            "structuredContent": {"job_id": str(row["id"]), "status": "complete"},
                            "isError": False}
                if name == "job_result" and row["status"] in ("failed", "cancelled"):
                    return {"content": [{"type": "text", "text": f"{row['status']}: {row['error'] or ''}"}],
                            "structuredContent": {"job_id": str(row["id"]), "status": row["status"]},
                            "isError": True}
                return await _job_handle(conn, principal, raw_id)
        if name == "elevate":
            from datum_sync import device
            from datum_sync.oauth import OAuthError

            scope = args.get("scope")
            if not isinstance(scope, str):
                raise RpcError(INVALID_PARAMS, "scope is required")
            try:
                async with db.pool().acquire() as conn:
                    body = await device.elevate(conn, principal, scope)
            except OAuthError as exc:
                return {"content": [{"type": "text", "text": f"{exc.error}: {exc.description}"}],
                        "isError": True}
            text = (
                "Approved automatically." if body["auto_approved"] else
                f"Ask a person to approve user code {body['user_code']} at "
                f"{body['verification_uri']}."
            ) + (
                f"\nThen POST {config.PUBLIC_URL}/oauth/token with grant_type="
                f"{device.DEVICE_GRANT}, client_id={device.ELEVATE_CLIENT_ID} and the "
                f"device_code below, no faster than every {body['interval']}s."
            )
            body.pop("elevation_id", None)
            return {"content": [{"type": "text", "text": text}],
                    "structuredContent": body, "isError": False}
        if name in ("pending_status", "pending_result"):
            async with db.pool().acquire() as conn:
                row = await pending.for_requester(conn, principal, args.get("pending_id"))
            body = pending.public(row)
            if name == "pending_status" or row["status"] in ("pending", "approved"):
                return {"content": [{"type": "text", "text": f"pending call {row['id']}: {row['status']}"}],
                        "structuredContent": body, "isError": False}
            if row["status"] in ("rejected", "expired"):
                return {"content": [{"type": "text", "text": f"access_denied: {row['status']}"
                                     + (f": {row['reason']}" if row["reason"] else "")}],
                        "structuredContent": {"code": "access_denied", **body}, "isError": True}
            result = auth.json_of(row, "result") or {}
            return {**result, "structuredContent": {**(result.get("structuredContent") or {}), "pending_id": row["id"]}}
        if name == "job_list":
            auth.require_tier(principal, 2, "job_list")
            limit = args.get("limit", 10)
            if not isinstance(limit, int) or not 1 <= limit <= 50:
                raise RpcError(INVALID_PARAMS, "limit must be 1-50")
            async with db.pool().acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT id, repository, workspace, status, submitted_at, completed_at
                      FROM jobs WHERE submitted_by = $1
                     ORDER BY submitted_at DESC LIMIT $2
                    """,
                    principal.name, limit,
                )
            body = [{"job_id": str(r["id"]), "workspace": f"{r['repository']}/{r['workspace']}",
                     "status": r["status"], "submitted_at": r["submitted_at"].isoformat()} for r in rows]
            return {"content": [{"type": "text", "text": json.dumps(body, indent=1) if body else "No jobs."}],
                    "structuredContent": {"jobs": body}, "isError": False}
    except ApiError as exc:
        return {
            "content": [{"type": "text", "text": f"{exc.code}: {exc.message}"}],
            "isError": True,
            "structuredContent": {"code": exc.code, **(exc.detail or {})},
        }
    raise RpcError(INVALID_PARAMS, f"unknown tool {name!r}")


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
    "resources/list": _resources_list,
    "resources/templates/list": _resources_templates_list,
    "resources/read": _resources_read,
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

    # -- sessions (spec 03 §5, 12 §2) ------------------------------------------
    # `initialize` admits a session and answers with its id; every other
    # method presents it. A session exists for counting and audit grouping
    # only -- the request is still fully authenticated by its bearer token.
    headers = {"MCP-Protocol-Version": PROTOCOL_VERSION}
    presented = request.headers.get(sessions.SESSION_HEADER)
    session_id: str | None = None
    if method == "initialize":
        try:
            async with db.pool().acquire() as conn:
                sid, superseded = await sessions.open(
                    conn, principal, params.get("clientInfo"), params.get("protocolVersion")
                )
        except sessions.SessionLimit as exc:
            await _log_call(principal, method, None, None, "error", SESSION_LIMIT, 0, trace)
            return _rpc_error(
                request_id, SESSION_LIMIT,
                f"session limit of {sessions.limit_for(principal)} reached; "
                "close a live session or wait for it to idle out",
                {"active": exc.active, "limit": sessions.limit_for(principal)},
            )
        session_id = str(sid)
        headers[sessions.SESSION_HEADER] = session_id
        if superseded:
            headers["X-Datum-Superseded-Session"] = superseded
    elif presented:
        async with db.pool().acquire() as conn:
            row = await sessions.touch(conn, principal, presented)
        if row is None:
            # Unknown, ended, idle, or another principal's: the transport
            # says 404 so the client re-initialises, and the body says
            # nothing more (SESS-002).
            return Response(status_code=404, headers=headers)
        session_id = presented
    elif principal.kind == "agent":
        # Agents always run in a session (SESS-004). Humans driving /mcp as a
        # plain RPC from a script get one release of grace.
        return JSONResponse(
            status_code=400,
            content={"status": 400, "code": "SESSION_REQUIRED",
                     "message": "send initialize first and present Mcp-Session-Id"},
            headers=headers,
        )
    trace = dataclasses.replace(trace, session_id=session_id)

    tool_name_val = params.get("name") if method == "tools/call" else None
    target = _call_target(method, tool_name_val, params)
    t0 = time.monotonic()

    try:
        result = await handler(principal, params, trace)
    except RpcError as exc:
        duration_ms = int((time.monotonic() - t0) * 1000)
        await _log_call(
            principal, method, tool_name_val, target,
            "error", exc.code, duration_ms, trace, session_id,
        )
        return _rpc_error(request_id, exc.code, exc.message, exc.data)

    duration_ms = int((time.monotonic() - t0) * 1000)
    await _log_call(
        principal, method, tool_name_val, target,
        "ok", None, duration_ms, trace, session_id,
    )
    return JSONResponse(
        content={"jsonrpc": "2.0", "id": request_id, "result": result},
        headers=headers,
    )


@router.delete("/mcp")
async def end_session(request: Request) -> Response:
    """The client is done with its session. Idempotent; always 204."""
    principal = await auth.require_auth(request)
    presented = request.headers.get(sessions.SESSION_HEADER)
    if presented:
        async with db.pool().acquire() as conn:
            await sessions.close(conn, principal, presented)
    return Response(status_code=204, headers={"MCP-Protocol-Version": PROTOCOL_VERSION})


@router.get("/mcp")
async def no_server_stream() -> Response:
    return Response(status_code=405, headers={"Allow": "POST"})
