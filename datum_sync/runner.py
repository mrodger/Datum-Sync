"""Runs one job in an isolated subprocess and pumps its events back.

The control plane never imports workspace code. It spawns
`python -m datum_sync.child`, writes the job spec to its stdin, and reads an
NDJSON event stream from its stdout.
"""
from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from datum_sync import config
from datum_sync.manifest import Manifest

# Environment handed to the child. DATABASE_URL is deliberately absent: a
# workspace must not be able to open a connection to the control plane's own
# database and rewrite the jobs table that is supervising it.
_ENV_PASSTHROUGH = ("PATH", "HOME", "LANG", "LC_ALL", "TZ", "SSL_CERT_FILE")

# Grace period between SIGTERM and SIGKILL on timeout or cancel.
KILL_GRACE_SECONDS = 5.0

_PR_SET_PDEATHSIG = 1

EventSink = Callable[[str, dict[str, Any]], Awaitable[None]]


def _child_preexec() -> None:  # pragma: no cover -- runs post-fork in the child
    """Ask the kernel to SIGKILL this process when the worker dies.

    Without this, a SIGKILLed worker leaves its children running: they are in
    their own session, so nothing else signals them. The next worker calls
    requeue_orphans(), starts a *second* child for the same job, and two
    processes write the same artifact directory -- for a workspace that shells
    out to QGIS, two renders racing over one PDF.

    Linux-only; elsewhere this is a no-op rather than a failed run.
    """
    try:
        import ctypes

        ctypes.CDLL("libc.so.6", use_errno=True).prctl(
            _PR_SET_PDEATHSIG, signal.SIGKILL
        )
    except Exception:
        return
    # The worker may have died between fork and prctl, in which case the
    # signal just armed will never arrive.
    if os.getppid() == 1:
        os._exit(1)


@dataclass
class RunResult:
    status: str                                   # complete | failed | cancelled
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None


def child_env() -> dict[str, str]:
    env = {k: os.environ[k] for k in _ENV_PASSTHROUGH if k in os.environ}
    env["PYTHONUNBUFFERED"] = "1"
    # The child imports datum_sync.child, so it needs the package importable
    # without inheriting the rest of the parent's environment.
    env["PYTHONPATH"] = str(config.REPO_ROOT)
    return env


class Runner:
    """One workspace run. Not reusable -- construct one per job."""

    def __init__(
        self,
        workspace_path: Path,
        manifest: Manifest,
        params: dict[str, Any],
        artifact_dir: Path,
        sink: EventSink,
        connections: dict[str, Any] | None = None,
    ) -> None:
        self.workspace_path = workspace_path
        self.manifest = manifest
        self.params = params
        self.artifact_dir = artifact_dir
        self.sink = sink
        self.connections = connections or {}
        self._proc: asyncio.subprocess.Process | None = None
        self._cancelled = False

    async def cancel(self) -> None:
        """Ask the child to stop. Safe to call before or after it has exited."""
        self._cancelled = True
        await self._terminate()

    async def _terminate(self) -> None:
        proc = self._proc
        if proc is None or proc.returncode is not None:
            return
        if not self._signal_group(signal.SIGTERM):
            return
        try:
            await asyncio.wait_for(proc.wait(), timeout=KILL_GRACE_SECONDS)
        except asyncio.TimeoutError:
            # SIGTERM ignored, or the process is wedged in uninterruptible IO.
            if not self._signal_group(signal.SIGKILL):
                return
            # Reap it. Returning before the child is really dead is how a
            # "killed" job goes on holding its slot and its file handles.
            await proc.wait()

    def _signal_group(self, sig: int) -> bool:
        """Signal the child's whole process group. False if it is already gone.

        The group, not the process: start_new_session put the child in its own
        group precisely so that anything it spawned -- qgis_process, gdal,
        ogr2ogr -- is reachable. Signalling the child alone would reap the
        Python wrapper and leave the heavy work running, which is the opposite
        of what a timeout is for.
        """
        proc = self._proc
        assert proc is not None
        try:
            os.killpg(os.getpgid(proc.pid), sig)
            return True
        except (ProcessLookupError, PermissionError):
            return False

    async def run(self) -> RunResult:
        self.artifact_dir.mkdir(parents=True, exist_ok=True)

        self._proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "datum_sync.child",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=child_env(),
            cwd=str(self.workspace_path),
            start_new_session=True,      # own process group, so killpg reaches
                                         # anything the workspace spawns
            preexec_fn=_child_preexec,   # ...and the group dies with the worker
        )
        proc = self._proc

        spec = json.dumps(
            {
                "workspace_path": str(self.workspace_path),
                "artifact_dir": str(self.artifact_dir),
                "params": self.params,
                "connections": self.connections,
            }
        )
        assert proc.stdin is not None
        proc.stdin.write(spec.encode())
        await proc.stdin.drain()
        proc.stdin.close()

        collected: dict[str, Any] = {}
        pump = asyncio.gather(
            self._pump_events(proc.stdout, collected),
            self._pump_stderr(proc.stderr),
        )

        timeout = self.manifest.timeout_seconds
        try:
            await asyncio.wait_for(proc.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            await self._terminate()
            pump.cancel()
            await asyncio.gather(pump, return_exceptions=True)
            return RunResult(
                status="failed", error=f"timed out after {timeout}s"
            )

        await asyncio.gather(pump, return_exceptions=True)

        if self._cancelled:
            return RunResult(status="cancelled", error="cancelled")

        if "error" in collected:
            return RunResult(status="failed", error=collected["error"])

        if proc.returncode != 0:
            return RunResult(
                status="failed",
                error=f"child exited {proc.returncode} without reporting an error",
            )

        try:
            artifacts = self._reconcile(collected.get("artifacts", []))
        except ValueError as e:
            return RunResult(status="failed", error=str(e))

        return RunResult(status="complete", artifacts=artifacts)

    def _reconcile(self, artifacts: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Rebuild each descriptor from the manifest, keeping only the bytes.

        `type` and `primary` are taken from the declared output, never from
        what run() returned. The manifest is the published interface: a caller
        that asked for the primary output of this workspace decided what that
        meant by reading the manifest, so a run must not be able to answer with
        a different one. The workspace only gets to say which file it wrote.
        """
        declared = {o.name: o for o in self.manifest.outputs}
        undeclared = {a["name"] for a in artifacts} - set(declared)
        if undeclared:
            # An output the manifest omits cannot be routed to a service or
            # fetched by name, so accepting it yields a job that "succeeded"
            # and returned something no caller can reach.
            raise ValueError(
                "run() returned undeclared artifacts: " + ", ".join(sorted(undeclared))
            )

        for a in artifacts:
            is_service = declared[a["name"]].type.startswith("service/")
            is_dir = bool(a.get("dir"))
            if is_service and not is_dir:
                raise ValueError(
                    f"{a['name']!r} is declared {declared[a['name']].type} but the "
                    "run returned a file; a hosted service is a directory"
                )
            # The converse matters more. A directory landed under an ordinary
            # output would be fetched by GET .../artifacts/{name}, which builds
            # a FileResponse -- so it would be a 500 at download time for a job
            # that reported success. Refuse it while there is still a run to
            # attribute it to.
            if is_dir and not is_service:
                raise ValueError(
                    f"{a['name']!r} is a directory, but output {a['name']!r} is "
                    f"declared {declared[a['name']].type}; only service/* outputs "
                    "may be directories"
                )

        return [
            {
                "name": a["name"],
                "type": declared[a["name"]].type,
                "primary": declared[a["name"]].primary,
                "file": a["file"],
                "dir": bool(a.get("dir")),
                "size": a["size"],
            }
            for a in artifacts
        ]

    async def _pump_events(
        self, stream: asyncio.StreamReader | None, collected: dict[str, Any]
    ) -> None:
        if stream is None:
            return
        async for raw in stream:
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                await self.sink("log", {"level": "warn", "message": f"bad event: {line}"})
                continue

            kind = event.get("event")
            if kind == "emit":
                await self.sink(event.get("type", "log"), event.get("payload") or {})
            elif kind == "result":
                collected["artifacts"] = event.get("artifacts", [])
            elif kind == "error":
                collected["error"] = event.get("message", "unknown error")

    async def _pump_stderr(self, stream: asyncio.StreamReader | None) -> None:
        """Workspace prints and tracebacks land in the job log."""
        if stream is None:
            return
        async for raw in stream:
            line = raw.decode("utf-8", "replace").rstrip()
            if line:
                await self.sink("log", {"level": "info", "message": line})
