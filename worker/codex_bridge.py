"""Small JSONL bridge to a deliberately constrained Codex app-server process."""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
import json
import os
from pathlib import Path
from typing import Any


class CodexBridgeError(RuntimeError):
    pass


SYSTEM_PROMPT = """You are the demonstration worker for Datum Sync.
Use the Datum Sync MCP tools whenever they can answer the user's request. Datum Sync owns tool
 discovery, credentials, authorization, revocation, and audit. Never invent tool results and never
 attempt to reach an upstream service outside Datum Sync. If a tool is unavailable or denied,
 explain the governance result plainly. When the user asks for a durable report, visual, dataset, or other
 artifact, call whoami if needed and save it with resources_create under agents/<current-agent-name>/.
 Resources are private until resources_share explicitly grants another agent access. Never assume a shared
 resource is model context; call resources_read only when the user asks to use it. Keep answers concise and
 suitable for a product demo."""


class CodexBridge:
    """One Codex process and thread, scoped to one Datum Sync agent session."""

    def __init__(self, *, token: str, datum_url: str, model: str, cwd: Path, codex: Path,
                 persona: dict[str, Any] | None = None):
        self.token = token
        self.datum_url = datum_url.rstrip("/")
        self.model = model
        self.cwd = cwd
        self.codex = codex
        self.process: asyncio.subprocess.Process | None = None
        self.reader: asyncio.Task | None = None
        self.pending: dict[int, asyncio.Future] = {}
        self.notifications: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=512)
        self.request_id = 0
        self.thread_id: str | None = None
        self.stderr_tail: list[str] = []
        self.turn_lock = asyncio.Lock()
        self.persona = persona if isinstance(persona, dict) else {}

    def developer_instructions(self) -> str:
        prompt = self.persona.get("system_prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            return SYSTEM_PROMPT
        return SYSTEM_PROMPT + "\n\nSelected Datum persona:\n" + prompt.strip()[:4000]

    def command(self) -> list[str]:
        """Build an override-only profile with Datum Sync as the sole external tool plane."""
        mcp = (
            "mcp_servers={datum_sync={url=\"" + self.datum_url + "/mcp\","
            "bearer_token_env_var=\"DATUM_SYNC_TOKEN\",required=true,startup_timeout_sec=15}}"
        )
        return [
            str(self.codex),
            "-c", 'model="' + self.model + '"',
            "-c", 'sandbox_mode="danger-full-access"',
            "-c", 'approval_policy="never"',
            "-c", 'web_search="disabled"',
            "-c", "tools.web_search=false",
            "-c", "tools.view_image=false",
            "-c", "features.shell_tool=false",
            "-c", "features.unified_exec=false",
            "-c", "features.apps=false",
            "-c", "features.plugins=false",
            "-c", "features.browser_use=false",
            "-c", "features.computer_use=false",
            "-c", "features.image_generation=false",
            "-c", "features.skill_search=false",
            "-c", "features.multi_agent=false",
            "-c", mcp,
            "app-server", "--stdio",
        ]

    async def start(self) -> None:
        if self.process and self.process.returncode is None:
            return
        self.cwd.mkdir(parents=True, exist_ok=True)
        allowed = ("HOME", "PATH", "LANG", "LC_ALL", "TZ", "SSL_CERT_FILE",
                   "SSL_CERT_DIR", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY")
        env = {key: os.environ[key] for key in allowed if key in os.environ}
        env["DATUM_SYNC_TOKEN"] = self.token
        self.process = await asyncio.create_subprocess_exec(
            *self.command(), cwd=self.cwd, env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self.reader = asyncio.create_task(self._read_stdout())
        asyncio.create_task(self._read_stderr())
        try:
            await self.request("initialize", {
                "clientInfo": {
                    "name": "datum-federated-worker",
                    "title": "Datum Federated Worker",
                    "version": "0.1.0",
                },
            })
            await self.notify("initialized", {})
            response = await self.request("thread/start", {
                "model": self.model,
                "cwd": str(self.cwd),
                "approvalPolicy": "never",
                # Execution tools are disabled. This avoids a bubblewrap dependency on the demo host.
                "sandbox": "danger-full-access",
                "developerInstructions": self.developer_instructions(),
                "ephemeral": True,
                "serviceName": "datum_sync_demo",
            }, timeout=30)
            self.thread_id = response["thread"]["id"]
        except Exception:
            await self.close()
            raise

    async def close(self) -> None:
        process = self.process
        if not process:
            return
        if process.returncode is None:
            if process.stdin:
                process.stdin.close()
                try:
                    await process.stdin.wait_closed()
                except (BrokenPipeError, ConnectionResetError):
                    pass
            try:
                await asyncio.wait_for(process.wait(), timeout=3)
            except TimeoutError:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=2)
                except TimeoutError:
                    process.kill()
                    await process.wait()
        if self.reader:
            self.reader.cancel()
        self.process = None

    async def request(self, method: str, params: Any, *, timeout: int = 15) -> Any:
        if not self.process or not self.process.stdin or self.process.returncode is not None:
            detail = self.stderr_tail[-1] if self.stderr_tail else "process is not running"
            raise CodexBridgeError("Codex app-server is unavailable: " + detail)
        self.request_id += 1
        request_id = self.request_id
        future = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        await self._send({"id": request_id, "method": method, "params": params})
        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except TimeoutError as exc:
            self.pending.pop(request_id, None)
            raise CodexBridgeError(f"Codex did not answer {method} in time") from exc

    async def notify(self, method: str, params: Any) -> None:
        await self._send({"method": method, "params": params})

    async def _send(self, message: dict[str, Any]) -> None:
        assert self.process and self.process.stdin
        self.process.stdin.write((json.dumps(message, separators=(",", ":")) + "\n").encode())
        await self.process.stdin.drain()

    async def _read_stdout(self) -> None:
        assert self.process and self.process.stdout
        while line := await self.process.stdout.readline():
            try:
                message = json.loads(line)
            except ValueError:
                continue
            request_id = message.get("id")
            if request_id is not None and ("result" in message or "error" in message):
                future = self.pending.pop(request_id, None)
                if future and not future.done():
                    if "error" in message:
                        detail = message["error"].get("message", "Codex request failed")
                        future.set_exception(CodexBridgeError(str(detail)))
                    else:
                        future.set_result(message["result"])
                continue
            if request_id is not None and "method" in message:
                await self._send({
                    "id": request_id,
                    "error": {"code": -32000, "message": "Disabled by the Datum worker profile"},
                })
                continue
            try:
                self.notifications.put_nowait(message)
            except asyncio.QueueFull:
                await self.notifications.get()
                self.notifications.put_nowait(message)
        error = CodexBridgeError("Codex app-server stopped unexpectedly")
        for future in self.pending.values():
            if not future.done():
                future.set_exception(error)
        self.pending.clear()

    async def _read_stderr(self) -> None:
        assert self.process and self.process.stderr
        while line := await self.process.stderr.readline():
            value = line.decode(errors="replace").strip()
            if value:
                self.stderr_tail.append(value[-500:])
                self.stderr_tail[:] = self.stderr_tail[-10:]

    async def mcp_status(self) -> dict[str, Any]:
        await self.start()
        return await self.request("mcpServerStatus/list", {"limit": 20})

    async def stream_turn(self, text: str) -> AsyncIterator[dict[str, Any]]:
        async with self.turn_lock:
            await self.start()
            if not self.thread_id:
                raise CodexBridgeError("Codex thread was not created")
            while not self.notifications.empty():
                self.notifications.get_nowait()
            response = await self.request("turn/start", {
                "threadId": self.thread_id,
                "input": [{"type": "text", "text": text}],
            }, timeout=30)
            turn_id = response["turn"]["id"]
            yield {"type": "turn", "turn_id": turn_id, "model": self.model}
            while True:
                try:
                    message = await asyncio.wait_for(self.notifications.get(), timeout=120)
                except TimeoutError as exc:
                    raise CodexBridgeError("Codex turn timed out") from exc
                method = message.get("method")
                params = message.get("params") or {}
                if params.get("turnId") not in (None, turn_id) and method != "turn/completed":
                    continue
                event = self.normalize_notification(method, params, turn_id)
                if event:
                    yield event
                if method == "turn/completed" and params.get("turn", {}).get("id") == turn_id:
                    return

    @staticmethod
    def normalize_notification(method: str | None, params: dict[str, Any], turn_id: str) -> dict[str, Any] | None:
        if method == "item/agentMessage/delta":
            return {"type": "delta", "text": str(params.get("delta", ""))}
        if method in ("item/started", "item/completed"):
            item = params.get("item") or {}
            if item.get("type") != "mcpToolCall":
                return None
            return {
                "type": "tool",
                "phase": "started" if method.endswith("started") else "completed",
                "server": str(item.get("server", "datum_sync")),
                "tool": str(item.get("tool", "unknown")),
                "status": str(item.get("status", "inProgress")),
                "duration_ms": item.get("durationMs"),
                "has_error": bool(item.get("error")),
            }
        if method == "turn/completed" and params.get("turn", {}).get("id") == turn_id:
            turn = params["turn"]
            return {
                "type": "done", "status": str(turn.get("status", "completed")),
                "error": bool(turn.get("error")),
            }
        if method == "error" and params.get("turnId") in (None, turn_id):
            return {"type": "error", "detail": str(params.get("message", "Codex turn failed"))[:500]}
        return None
