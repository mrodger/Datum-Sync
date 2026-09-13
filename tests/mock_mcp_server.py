"""A mock upstream MCP server: github-, ssh- and drive-shaped tools.

An ASGI app speaking JSON-RPC over streamable HTTP, used two ways:

* by `tests/test_federation.py` through `httpx.ASGITransport`, so no port
  is opened and every upstream request is counted in `CALLS`;
* by `tests/browser_smoke.py` under uvicorn on a loopback port, so the
  Connections screen can save a real federation connection against it.

Every tool echoes its arguments and the `Authorization` header it was
given, which is how the tests see that the upstream credential was
injected on the way out and scrubbed on the way back.

    python tests/mock_mcp_server.py [port]
"""
from __future__ import annotations

import json
import sys
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

SESSION = "mock-session-1"

# Every upstream request that reached a tool, oldest first. Tests reset it.
CALLS: list[dict[str, Any]] = []
# Flip to make the server refuse everything, the way a stopped one does.
DOWN = False

TOOLS = {
    "github": [
        {"name": "get_file_contents", "description": "Read a file from a repository",
         "inputSchema": {"type": "object", "properties": {"owner": {"type": "string"}, "repo": {"type": "string"},
                                                          "path": {"type": "string"}},
                         "required": ["owner", "repo", "path"]}},
        {"name": "push_files", "description": "Push files to a branch",
         "inputSchema": {"type": "object", "properties": {
             "owner": {"type": "string"}, "repo": {"type": "string"}, "branch": {"type": "string"},
             "files": {"type": "array", "items": {"type": "object", "properties": {
                 "path": {"type": "string"}, "content": {"type": "string"}}}}},
             "required": ["owner", "repo", "files"]}},
        {"name": "delete_repository", "description": "Delete a repository",
         "inputSchema": {"type": "object", "properties": {"owner": {"type": "string"}, "repo": {"type": "string"}}}},
        {"name": "list_commits", "description": "List commits",
         "inputSchema": {"type": "object", "properties": {"owner": {"type": "string"}, "repo": {"type": "string"}}}},
        {"name": "get_me", "description": "The authenticated user", "inputSchema": {"type": "object", "properties": {}}},
    ],
    "ssh": [
        {"name": "run_command", "description": "Run a command on a host",
         "inputSchema": {"type": "object", "properties": {"host": {"type": "string"}, "command": {"type": "string"}},
                         "required": ["host", "command"]}},
        {"name": "read_file", "description": "Read a file on a host",
         "inputSchema": {"type": "object", "properties": {"host": {"type": "string"}, "path": {"type": "string"}}}},
        {"name": "write_file", "description": "Write a file on a host",
         "inputSchema": {"type": "object", "properties": {"host": {"type": "string"}, "path": {"type": "string"},
                                                          "content": {"type": "string"}}}},
        {"name": "restart_service", "description": "Restart a service",
         "inputSchema": {"type": "object", "properties": {"host": {"type": "string"}, "service": {"type": "string"}}}},
    ],
    "drive": [
        {"name": "get_file", "description": "A file's metadata",
         "inputSchema": {"type": "object", "properties": {"fileId": {"type": "string"}}, "required": ["fileId"]}},
        {"name": "search", "description": "Search a folder",
         "inputSchema": {"type": "object", "properties": {"folderId": {"type": "string"}, "q": {"type": "string"}}}},
        {"name": "share_file", "description": "Share a file",
         "inputSchema": {"type": "object", "properties": {"fileId": {"type": "string"}, "with": {"type": "string"}}}},
    ],
}

# Drive folder tree for `resolve: drive_folder`: file -> parent.
PARENTS = {"file-in-granted": "folder-granted", "file-nested": "sub-folder", "sub-folder": "folder-granted",
           "file-elsewhere": "folder-other", "file-orphan": None}

RESOURCES = [
    {"uri": "repo://datum/gateway/README.md", "name": "gateway README", "mimeType": "text/markdown"},
    {"uri": "repo://other/repo/README.md", "name": "other README", "mimeType": "text/markdown"},
]


def _flavour(request: Request) -> str:
    return request.path_params.get("flavour") or "github"


async def mcp(request: Request) -> Response:
    if DOWN:
        return Response(status_code=503)
    body = await request.json()
    method, params, rid = body.get("method"), body.get("params") or {}, body.get("id")
    if rid is None:
        return Response(status_code=202)
    flavour = _flavour(request)
    headers = {"Mcp-Session-Id": SESSION}

    def ok(result):
        return JSONResponse({"jsonrpc": "2.0", "id": rid, "result": result}, headers=headers)

    def err(code, message):
        return JSONResponse({"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}, headers=headers)

    if method == "initialize":
        return ok({"protocolVersion": "2025-06-18", "capabilities": {"tools": {}, "resources": {}},
                   "serverInfo": {"name": f"mock-{flavour}", "version": "0"}})
    if method == "tools/list":
        return ok({"tools": TOOLS[flavour]})
    if method == "resources/list":
        return ok({"resources": RESOURCES if flavour == "github" else []})
    if method == "resources/templates/list":
        return ok({"resourceTemplates": []})
    if method == "resources/read":
        CALLS.append({"method": method, "uri": params.get("uri"),
                      "authorization": request.headers.get("authorization")})
        return ok({"contents": [{"uri": params.get("uri"), "mimeType": "text/markdown", "text": "# hello"}]})
    if method == "tools/call":
        name, args = params.get("name"), params.get("arguments") or {}
        if name not in {t["name"] for t in TOOLS[flavour]}:
            return err(-32602, f"unknown tool {name}")
        CALLS.append({"method": method, "tool": name, "arguments": args,
                      "authorization": request.headers.get("authorization")})
        if name == "get_file":
            fid = args.get("fileId")
            if fid not in PARENTS:
                return ok({"content": [{"type": "text", "text": "not found"}], "isError": True})
            parent = PARENTS[fid]
            return ok({"content": [{"type": "text", "text": json.dumps({"id": fid, "parents": [parent] if parent else []})}],
                       "structuredContent": {"id": fid, "parents": [parent] if parent else []}})
        echo = {"tool": name, "arguments": args, "authorization": request.headers.get("authorization")}
        if name == "get_file_contents":
            echo["content"] = "x" * int(args.get("_size", 0) or 0)
        return ok({"content": [{"type": "text", "text": json.dumps(echo)}], "structuredContent": echo})
    return err(-32601, f"unknown method {method}")


app = Starlette(routes=[Route("/mcp", mcp, methods=["POST"]), Route("/{flavour}/mcp", mcp, methods=["POST"])])


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=int(sys.argv[1]) if len(sys.argv) > 1 else 8299, log_level="warning")
