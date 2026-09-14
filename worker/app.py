"""Datum-branded federated worker powered by Codex and governed by Datum Sync."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
from pathlib import Path
import secrets
import time
from datetime import datetime, timezone
from urllib.parse import urlencode

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from codex_bridge import CodexBridge, CodexBridgeError
from datum_sync_client import DatumSyncMCP, DatumSyncError

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parent
BRANDING = ROOT / "static" / "branding"
load_dotenv(ROOT / ".env")
DATUM_SYNC_URL = os.environ.get("DATUM_SYNC_URL", "http://127.0.0.1:8210").rstrip("/")
WORKER_URL = os.environ.get("WORKER_URL", "http://127.0.0.1:8220").rstrip("/")
CODEX_MODEL = os.environ.get("DATUM_WORKER_MODEL", "gpt-5.6-luna")
CODEX_BIN = Path(os.environ.get("CODEX_BIN", str(Path.home() / ".local/bin/codex")))
COOKIE = "datum_worker_session"

app = FastAPI(title="Datum Federated Worker", version="0.4.0")
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
app.mount(
    "/assets/fonts",
    StaticFiles(directory=BRANDING / "fonts"),
    name="branding-fonts",
)
_pending: dict[str, dict] = {}
_sessions: dict[str, dict] = {}


def _prune() -> None:
    current = time.time()
    for key, value in list(_pending.items()):
        if value["created_at"] < current - 600:
            _pending.pop(key, None)
    for key, value in list(_sessions.items()):
        if value["authorization_expires_epoch"] <= current:
            session = _sessions.pop(key)
            bridge = session.get("bridge")
            if bridge:
                asyncio.get_running_loop().create_task(bridge.close())


def _deadline_epoch(value: str | None) -> float:
    if not value:
        raise HTTPException(502, "Datum Sync did not return a session deadline.")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc).timestamp()
    except (TypeError, ValueError) as exc:
        raise HTTPException(502, "Datum Sync returned an invalid session deadline.") from exc


async def _access(item: dict) -> str:
    """Return a live access token, rotating it without changing the worker session."""
    if item["authorization_expires_epoch"] <= time.time():
        raise HTTPException(401, "The selected Datum Sync agent session has expired.")
    if item["access_expires_epoch"] > time.time() + 45:
        return item["access_token"]
    async with item["refresh_lock"]:
        if item["access_expires_epoch"] > time.time() + 45:
            return item["access_token"]
        token = await _datum_request("POST", "/oauth/token", data={
            "grant_type": "refresh_token", "refresh_token": item["refresh_token"],
            "client_id": item["client_id"],
        })
        item["access_token"] = token["access_token"]
        item["refresh_token"] = token["refresh_token"]
        item["access_expires_epoch"] = time.time() + int(token.get("expires_in", 0))
        item["authorization_expires_at"] = token.get("authorization_expires_at", item["authorization_expires_at"])
        return item["access_token"]


def _pkce_challenge(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")


def _session(request: Request) -> dict:
    _prune()
    item = _sessions.get(request.cookies.get(COOKIE, ""))
    if not item:
        raise HTTPException(401, "Connect this worker to a Datum Sync agent first.")
    return item


async def _datum_request(method: str, path: str, **kwargs):
    async with httpx.AsyncClient(base_url=DATUM_SYNC_URL, timeout=15,
                                 follow_redirects=False, trust_env=False) as client:
        try:
            response = await client.request(method, path, **kwargs)
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise HTTPException(502, "Datum Sync is unavailable or rejected the request.") from exc


@app.middleware("http")
async def browser_headers(request: Request, call_next):
    response = await call_next(request)
    if request.url.path.startswith("/api/artifacts/") and request.url.path.endswith("/content"):
        response.headers["Content-Security-Policy"] = (
            "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
            "img-src data: blob:; connect-src 'none'; base-uri 'none'; form-action 'none'"
        )
    else:
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; "
            "connect-src 'self'; base-uri 'none'; frame-ancestors 'none'"
        )
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


@app.on_event("shutdown")
async def close_workers() -> None:
    for session in list(_sessions.values()):
        bridge = session.get("bridge")
        if bridge:
            await bridge.close()


@app.get("/")
async def index():
    return FileResponse(ROOT / "static" / "index.html")


@app.get("/assets/harness.css")
async def harness_styles():
    return FileResponse(BRANDING / "harness.css", media_type="text/css")


@app.get("/assets/datum-mark.svg")
async def datum_mark():
    return FileResponse(BRANDING / "datum-mark.svg", media_type="image/svg+xml")


@app.get("/health")
async def health():
    datum_ok = False
    try:
        status = await _datum_request("GET", "/health")
        datum_ok = status.get("status") == "ok"
    except HTTPException:
        pass
    return {
        "status": "ok", "datum_sync": datum_ok,
        "codex_installed": CODEX_BIN.is_file(), "model": CODEX_MODEL,
    }


@app.get("/connect")
async def connect():
    _prune()
    redirect_uri = WORKER_URL + "/oauth/callback"
    registration = await _datum_request("POST", "/oauth/register", json={
        "client_name": "Datum Codex Worker", "redirect_uris": [redirect_uri],
    })
    verifier = secrets.token_urlsafe(48)
    state = secrets.token_urlsafe(24)
    _pending[state] = {
        "created_at": time.time(), "verifier": verifier,
        "client_id": registration["client_id"], "redirect_uri": redirect_uri,
    }
    query = urlencode({
        "response_type": "code", "client_id": registration["client_id"],
        "redirect_uri": redirect_uri, "scope": "mcp",
        "resource": DATUM_SYNC_URL + "/mcp",
        "code_challenge": _pkce_challenge(verifier), "code_challenge_method": "S256",
        "state": state,
    })
    return RedirectResponse(DATUM_SYNC_URL + "/oauth/authorize?" + query, status_code=303)


@app.get("/oauth/callback")
async def oauth_callback(code: str, state: str):
    pending = _pending.pop(state, None)
    if not pending or pending["created_at"] < time.time() - 600:
        raise HTTPException(400, "OAuth state is missing or expired.")
    token = await _datum_request("POST", "/oauth/token", data={
        "grant_type": "authorization_code", "code": code,
        "client_id": pending["client_id"], "redirect_uri": pending["redirect_uri"],
        "code_verifier": pending["verifier"],
    })
    me = await _datum_request("GET", "/api/me", headers={
        "Authorization": "Bearer " + token["access_token"],
    })
    principal = me.get("user") or {}
    persona = principal.get("persona") if isinstance(principal.get("persona"), dict) else None
    sid = secrets.token_urlsafe(32)
    deadline = _deadline_epoch(token.get("authorization_expires_at"))
    proxy_token = secrets.token_urlsafe(32)
    bridge = CodexBridge(
        token=proxy_token, datum_url=WORKER_URL + "/internal/mcp/" + sid, model=CODEX_MODEL,
        cwd=REPO / ".runtime" / "worker-workspaces" / sid, codex=CODEX_BIN,
        persona=persona,
    )
    _sessions[sid] = {
        "created_at": time.time(), "access_token": token["access_token"],
        "refresh_token": token["refresh_token"], "client_id": pending["client_id"],
        "access_expires_epoch": time.time() + int(token.get("expires_in", 0)),
        "authorization_expires_at": token.get("authorization_expires_at"),
        "authorization_expires_epoch": deadline, "proxy_token": proxy_token,
        "refresh_lock": asyncio.Lock(), "bridge": bridge, "principal": principal,
    }
    response = RedirectResponse("/", status_code=303)
    response.set_cookie(COOKIE, sid, httponly=True, samesite="lax",
                        max_age=max(1, int(deadline - time.time())))
    return response


@app.get("/api/status")
async def status(request: Request):
    item = _sessions.get(request.cookies.get(COOKIE, ""))
    principal = None
    if item:
        try:
            token = await _access(item)
            me = await _datum_request("GET", "/api/me", headers={
                "Authorization": "Bearer " + token,
            })
            user = me.get("user") or {}
            principal = {key: user.get(key) for key in ("name", "display_name", "state", "persona")
                         if user.get(key) is not None}
        except HTTPException:
            expired = _sessions.pop(request.cookies.get(COOKIE, ""), None)
            if expired and expired.get("bridge"):
                await expired["bridge"].close()
            item = None
    return {
        "connected": bool(principal), "principal": principal,
        "datum_sync_url": DATUM_SYNC_URL, "model": CODEX_MODEL,
        "codex_installed": CODEX_BIN.is_file(),
        "authorization_expires_at": item.get("authorization_expires_at") if item else None,
    }


@app.post("/api/disconnect")
async def disconnect(request: Request):
    item = _sessions.pop(request.cookies.get(COOKIE, ""), None)
    if item and item.get("bridge"):
        await item["bridge"].close()
    response = RedirectResponse("/", status_code=303)
    response.delete_cookie(COOKIE)
    return response


@app.api_route("/internal/mcp/{sid}/mcp", methods=["GET", "POST", "DELETE"])
async def internal_mcp(sid: str, request: Request):
    """Relay Codex MCP traffic through the worker's rotating Datum credential."""
    item = _sessions.get(sid)
    supplied = request.headers.get("authorization", "")
    expected = "Bearer " + item["proxy_token"] if item else ""
    if not item or not secrets.compare_digest(supplied, expected):
        raise HTTPException(401, "Invalid worker MCP session.")
    token = await _access(item)
    headers = {"Authorization": "Bearer " + token}
    for name in ("content-type", "accept", "mcp-session-id"):
        if request.headers.get(name):
            headers[name] = request.headers[name]
    async with httpx.AsyncClient(base_url=DATUM_SYNC_URL, timeout=130,
                                 follow_redirects=False, trust_env=False) as client:
        try:
            upstream = await client.request(request.method, "/mcp", content=await request.body(), headers=headers)
        except httpx.HTTPError as exc:
            raise HTTPException(502, "Datum Sync MCP relay is unavailable.") from exc
    returned = {name: value for name, value in upstream.headers.items()
                if name.lower() in {"content-type", "mcp-session-id", "x-trace-id"}}
    return Response(content=upstream.content, status_code=upstream.status_code, headers=returned)


@app.get("/api/tools")
async def tools(request: Request):
    item = _session(request)
    try:
        async with DatumSyncMCP(DATUM_SYNC_URL, await _access(item)) as mcp:
            catalogue = await mcp.tools()
    except DatumSyncError as exc:
        raise HTTPException(502, str(exc)) from exc
    return {"tools": [tool["function"] for tool in catalogue]}


@app.get("/api/credentials")
async def credentials(request: Request):
    """Return the connected agent's safe managed-credential view."""
    item = _session(request)
    catalogue = await _datum_request(
        "GET", "/api/credential-catalog",
        headers={"Authorization": "Bearer " + await _access(item)},
    )
    safe_items = []
    for entry in catalogue.get("items", []):
        if not isinstance(entry, dict):
            continue
        safe_items.append({
            "connection": entry.get("connection"),
            "label": entry.get("label"),
            "auth_type": entry.get("auth_type"),
            "requestable_tools": entry.get("requestable_tools", []),
            "granted_tools": entry.get("granted_tools", []),
            "pending_request": bool(entry.get("pending_request_id")),
            "expires_at": entry.get("expires_at"),
        })
    return {"items": safe_items}


@app.get("/api/artifacts")
async def artifacts(request: Request):
    item = _session(request)
    return await _datum_request("GET", "/api/resources", headers={
        "Authorization": "Bearer " + await _access(item),
    })


@app.get("/api/artifacts/{artifact_id}/content")
async def artifact_content(artifact_id: str, request: Request):
    item = _session(request)
    async with httpx.AsyncClient(base_url=DATUM_SYNC_URL, timeout=15,
                                 follow_redirects=False, trust_env=False) as client:
        try:
            upstream = await client.get("/api/resources/" + artifact_id + "/content", headers={
                "Authorization": "Bearer " + await _access(item),
            })
            upstream.raise_for_status()
        except httpx.HTTPError as exc:
            raise HTTPException(502, "Datum Sync is unavailable or rejected the artifact.") from exc
    return Response(content=upstream.content,
                    media_type=upstream.headers.get("content-type", "text/plain"),
                    headers={"Cache-Control": "private, no-store", "Content-Disposition": "inline"})


@app.get("/api/codex-mcp")
async def codex_mcp(request: Request):
    item = _session(request)
    try:
        return await item["bridge"].mcp_status()
    except CodexBridgeError as exc:
        raise HTTPException(502, str(exc)) from exc


@app.post("/api/chat")
async def chat(request: Request):
    item = _session(request)
    body = await request.json()
    message = body.get("message")
    if not isinstance(message, str) or not message.strip():
        raise HTTPException(400, "message must be non-empty text")
    if len(message.encode()) > 32_000:
        raise HTTPException(413, "message is too large")

    async def events():
        try:
            async for event in item["bridge"].stream_turn(message.strip()):
                yield _sse(event["type"], {k: v for k, v in event.items() if k != "type"})
        except (CodexBridgeError, DatumSyncError) as exc:
            yield _sse("error", {"detail": str(exc)})
        except Exception:
            yield _sse("error", {"detail": "The Codex worker stopped unexpectedly."})

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


def _sse(kind: str, value: dict) -> str:
    return "event: " + kind + "\ndata: " + json.dumps(value, default=str) + "\n\n"
