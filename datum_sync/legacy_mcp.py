"""Loopback MCP adapter exposing read-only metadata from the legacy service."""
from __future__ import annotations

from contextlib import asynccontextmanager

import json
import os
import secrets
import uuid

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from datum_sync import config, db

PROTOCOL = "2025-06-18"
TOOLS = [
    {"name": "list_repositories", "description": "List legacy Datum-Sync repositories and their publication state.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "list_workspaces", "description": "List legacy workspaces and publication state, optionally within one repository.",
     "inputSchema": {"type": "object", "properties": {"repository": {"type": "string"}}}},
    {"name": "workspace_manifest", "description": "Read a legacy workspace manifest and publication state without executing it.",
     "inputSchema": {"type": "object", "properties": {"repository": {"type": "string"}, "workspace": {"type": "string"}},
                     "required": ["repository", "workspace"]}},
    {"name": "job_summary", "description": "Count the caller's jobs by status.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "list_jobs", "description": "List recent jobs submitted by the caller or its descendants.",
     "inputSchema": {"type": "object", "properties": {
         "status": {"type": "string", "enum": ["queued", "running", "complete", "failed", "cancelled"]},
         "repository": {"type": "string"}, "workspace": {"type": "string"},
         "limit": {"type": "integer", "minimum": 1, "maximum": 100}}}},
    {"name": "job_status", "description": "Read status for one visible job without returning its parameters.",
     "inputSchema": {"type": "object", "properties": {"job_id": {"type": "string", "format": "uuid"}},
                     "required": ["job_id"]}},
    {"name": "job_log", "description": "Read a bounded page of log entries for one visible job.",
     "inputSchema": {"type": "object", "properties": {
         "job_id": {"type": "string", "format": "uuid"},
         "after_id": {"type": "integer", "minimum": 0},
         "limit": {"type": "integer", "minimum": 1, "maximum": 200}}, "required": ["job_id"]}},
    {"name": "job_events", "description": "Read a durable page of status, progress and log events for one visible job.",
     "inputSchema": {"type": "object", "properties": {
         "job_id": {"type": "string", "format": "uuid"},
         "after_id": {"type": "integer", "minimum": 0},
         "limit": {"type": "integer", "minimum": 1, "maximum": 200}}, "required": ["job_id"]}},
    {"name": "job_artifacts", "description": "List safe artifact metadata for one visible job without downloading content.",
     "inputSchema": {"type": "object", "properties": {"job_id": {"type": "string", "format": "uuid"}},
                     "required": ["job_id"]}},
]

@asynccontextmanager
async def lifespan(_app):
    if len(os.environ.get("LEGACY_MCP_TOKEN", "")) < 32:
        raise RuntimeError("LEGACY_MCP_TOKEN must be configured")
    await db.init_pool()
    try:
        yield
    finally:
        await db.close_pool()


app = FastAPI(title="Datum legacy MCP adapter", version="0.3.0", lifespan=lifespan)


def rpc_error(rid, code, message):
    return JSONResponse({"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}})


def authenticated(request: Request) -> bool:
    supplied = request.headers.get("authorization", "")
    expected = "Bearer " + os.environ["LEGACY_MCP_TOKEN"]
    return secrets.compare_digest(supplied, expected)


@app.get("/health")
async def health(request: Request):
    if not authenticated(request):
        return JSONResponse({"detail": "unauthorized"}, status_code=401)
    async with db.pool().acquire() as conn:
        await conn.fetchval("SELECT 1")
    return {"status": "ok", "service": "datum-legacy-mcp"}


@app.post("/mcp")
async def mcp(request: Request):
    if not authenticated(request):
        return JSONResponse({"detail": "unauthorized"}, status_code=401)
    try:
        body = await request.json()
    except ValueError:
        return rpc_error(None, -32700, "Parse error")
    rid, method = body.get("id"), body.get("method")
    if body.get("jsonrpc") != "2.0":
        return rpc_error(rid, -32600, "Invalid request")
    if method == "initialize":
        result = {"protocolVersion": PROTOCOL, "capabilities": {"tools": {"listChanged": False}},
                  "serverInfo": {"name": "datum-legacy", "version": "0.3.0"}}
    elif method == "notifications/initialized":
        return JSONResponse({}, status_code=202)
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        params = body.get("params") or {}
        meta = params.get("_meta") if isinstance(params.get("_meta"), dict) else {}
        principal = meta.get("io.datum.principal")
        result = await call(params.get("name"), params.get("arguments") or {}, principal)
        if result is None:
            return rpc_error(rid, -32602, "Unknown tool")
    else:
        return rpc_error(rid, -32601, "Method not found")
    return {"jsonrpc": "2.0", "id": rid, "result": result}


async def call(name, arguments, principal=None):
    if not isinstance(arguments, dict):
        return denied("arguments must be an object")
    async with db.pool().acquire() as conn:
        scope = await principal_scope(conn, principal)
        if name in {tool["name"] for tool in TOOLS} and not scope:
            return denied("principal context is missing or inactive")
        if name == "list_repositories":
            published={row['name'] for row in await conn.fetch("SELECT name FROM repositories")}
            items=[]
            for repository in sorted(config.REPOSITORIES_PATH.iterdir()):
                if not repository.is_dir():
                    continue
                manifests=list(repository.glob('*/manifest.json'))
                if manifests and repository_allowed(scope,repository.name):
                    items.append({'name':repository.name,'workspace_count':len(manifests),
                                  'published':repository.name in published})
            return content({"repositories":items})
        if name == "list_workspaces":
            repository = arguments.get("repository")
            if repository is not None and (not isinstance(repository, str) or len(repository) > 200):
                return denied("repository must be a string")
            if repository is not None and not repository_allowed(scope,repository):
                return content({"workspaces":[]})
            published={(row['repository'],row['workspace']) for row in await conn.fetch(
                "SELECT r.name AS repository,w.name AS workspace FROM workspaces w JOIN repositories r ON r.id=w.repository_id")}
            items=[]
            roots=[config.REPOSITORIES_PATH/repository] if repository else sorted(config.REPOSITORIES_PATH.iterdir())
            for root in roots:
                if not root.is_dir() or root.parent != config.REPOSITORIES_PATH or not repository_allowed(scope,root.name):
                    continue
                for manifest in sorted(root.glob('*/manifest.json')):
                    data=json.loads(manifest.read_text())
                    items.append({'repository':root.name,'workspace':manifest.parent.name,
                                  'description':data.get('description',''),
                                  'published':(root.name,manifest.parent.name) in published})
            return content({"workspaces":items[:500]})
        if name == "workspace_manifest":
            repository, workspace = arguments.get("repository"), arguments.get("workspace")
            if not repository_allowed(scope,repository):
                return denied("workspace not found")
            if not all(isinstance(value, str) and 0 < len(value) <= 200 for value in (repository, workspace)):
                return denied("repository and workspace are required")
            if any('/' in value or chr(92) in value or value in ('.','..') for value in (repository,workspace)):
                return denied("invalid workspace name")
            source=config.REPOSITORIES_PATH/repository/workspace/'manifest.json'
            if source.is_file() and source.parent.parent.parent == config.REPOSITORIES_PATH:
                manifest=json.loads(source.read_text())
                published=bool(await conn.fetchval("""SELECT 1 FROM workspaces w JOIN repositories r ON r.id=w.repository_id
                    WHERE r.name=$1 AND w.name=$2""",repository,workspace))
                return content({'repository':repository,'workspace':workspace,'published':published,'manifest':manifest})
            return denied("workspace not found")
        if name in ("job_summary", "list_jobs", "job_status", "job_log", "job_events", "job_artifacts"):
            ids, visible = scope["ids"], scope["names"]
            repos, all_repos = scope["repositories"], scope["all_repositories"]
            ownership = "(portal_principal_id=ANY($1::int[]) OR (portal_principal_id IS NULL AND submitted_by=ANY($2::text[])))"
            if name == "job_summary":
                rows = await conn.fetch(f"""SELECT status,count(*) AS n FROM jobs WHERE {ownership}
                    AND ($3::boolean OR repository=ANY($4::text[])) GROUP BY status""", ids, visible, all_repos, repos)
                counts = dict.fromkeys(("queued","running","complete","failed","cancelled"), 0)
                for row in rows:
                    counts[row["status"]] = row["n"]
                return content({"counts": counts, "total": sum(row["n"] for row in rows)})
            if name == "list_jobs":
                status = arguments.get("status")
                repository = arguments.get("repository")
                workspace = arguments.get("workspace")
                limit = arguments.get("limit", 50)
                if status is not None and status not in ("queued","running","complete","failed","cancelled"):
                    return denied("invalid job status")
                if any(value is not None and (not isinstance(value,str) or not value or len(value)>200)
                       for value in (repository,workspace)):
                    return denied("repository and workspace filters must be non-empty strings")
                if repository is not None and not repository_allowed(scope,repository):
                    return content({"jobs":[],"count":0})
                if type(limit) is not int or not 1 <= limit <= 100:
                    return denied("limit must be between 1 and 100")
                rows = await conn.fetch(f"""SELECT id,repository,workspace,status,submitted_by,left(error,4096) AS error,submitted_at,started_at,completed_at,
                    jsonb_array_length(artifacts) AS artifact_count FROM jobs WHERE {ownership}
                      AND ($3::boolean OR repository=ANY($4::text[])) AND ($5::text IS NULL OR status=$5)
                      AND ($6::text IS NULL OR repository=$6) AND ($7::text IS NULL OR workspace=$7)
                    ORDER BY submitted_at DESC LIMIT $8""", ids, visible, all_repos, repos, status, repository, workspace, limit)
                return content({"jobs": [job_value(row) for row in rows], "count": len(rows)})
            job_id = parse_job_id(arguments.get("job_id"))
            if job_id is None:
                return denied("job_id must be a UUID")
            row = await conn.fetchrow(f"""SELECT id,repository,workspace,status,submitted_by,left(error,4096) AS error,submitted_at,started_at,completed_at,
                jsonb_array_length(artifacts) AS artifact_count,artifacts FROM jobs WHERE id=$3 AND {ownership}
                AND ($4::boolean OR repository=ANY($5::text[]))""", ids, visible, job_id, all_repos, repos)
            if row is None:
                return denied("job not found")
            if name == "job_status":
                return content(job_value(row))
            if name == "job_artifacts":
                raw = json.loads(row["artifacts"])
                safe = [{key:item[key] for key in ("name","type","primary","dir","size") if key in item}
                        for item in raw if isinstance(item,dict)]
                return content({"job_id":str(job_id),"artifacts":safe,"count":len(safe)})
            after_id, limit = arguments.get("after_id", 0), arguments.get("limit", 100)
            if type(after_id) is not int or after_id < 0 or type(limit) is not int or not 1 <= limit <= 200:
                return denied("after_id or limit is invalid")
            if name == "job_events":
                entries = await conn.fetch("""SELECT id,kind,payload,created_at FROM job_events
                    WHERE job_id=$1 AND id>$2 ORDER BY id LIMIT $3""",job_id,after_id,limit)
                return content({"job_id":str(job_id),"events":[{"id":item["id"],"kind":item["kind"],
                    "payload":json.loads(item["payload"]),"created_at":item["created_at"].isoformat()} for item in entries],
                    "count":len(entries)})
            entries = await conn.fetch("""SELECT id,ts,level,left(message,4096) AS message FROM job_log
                WHERE job_id=$1 AND id>$2 ORDER BY id LIMIT $3""", job_id, after_id, limit)
            return content({"job_id": str(job_id), "entries": [
                {"id": row["id"], "ts": row["ts"].isoformat(), "level": row["level"], "message": row["message"]}
                for row in entries], "count": len(entries)})
    return None


async def principal_scope(conn, principal):
    if not isinstance(principal, dict) or type(principal.get("id")) is not int or not isinstance(principal.get("name"), str):
        return None
    repositories = principal.get("repositories")
    if not isinstance(repositories,list) or len(repositories)>50 or any(
            not isinstance(value,str) or not value or len(value)>200 for value in repositories):
        return None
    rows = await conn.fetch("""WITH RECURSIVE visible(id,name) AS (
        SELECT id,name FROM service_accounts
          WHERE id=$1 AND name=$2 AND disabled=false AND portal_state='active'
        UNION ALL
        SELECT child.id,child.name FROM service_accounts child JOIN visible parent ON child.portal_parent=parent.id
    ) SELECT id,name FROM visible""", principal["id"], principal["name"])
    if not rows:
        return None
    return {"ids":[row["id"] for row in rows],"names":[row["name"] for row in rows],
            "repositories":repositories,"all_repositories":"*" in repositories}


def repository_allowed(scope, repository):
    return bool(scope and isinstance(repository,str) and
                (scope["all_repositories"] or repository in scope["repositories"]))

def parse_job_id(value):
    try:
        return uuid.UUID(value) if isinstance(value, str) else None
    except ValueError:
        return None


def job_value(row):
    def stamp(key):
        return row[key].isoformat() if row[key] is not None else None
    return {"id": str(row["id"]), "repository": row["repository"], "workspace": row["workspace"],
            "status": row["status"], "submitted_by": row["submitted_by"], "error": row["error"],
            "artifact_count": row["artifact_count"], "submitted_at": stamp("submitted_at"),
            "started_at": stamp("started_at"), "completed_at": stamp("completed_at")}


def content(value):
    return {"content": [{"type": "text", "text": json.dumps(value, default=str)}],
            "structuredContent": value, "isError": False}


def denied(message):
    return {"content": [{"type": "text", "text": message}], "isError": True}
