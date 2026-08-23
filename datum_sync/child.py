"""Child-side harness. Runs one workspace, then exits.

    python -m datum_sync.child     < spec.json

Invoked by runner.py in a subprocess. This is the only place workspace code is
imported and executed; nothing here may touch the control plane's database.

The spec arrives on **stdin, never argv**: argv is world-readable through
/proc/<pid>/cmdline, so once connections carry credentials, passing them as
arguments would publish them to every user on the host.

File descriptor layout, established before the workspace is imported:

    fd 3-ish (dup of real stdout)  NDJSON event channel, parent reads it
    fd 1 -> fd 2                   anything the workspace prints
    fd 2                           child diagnostics -> job log

A workspace that calls print() is normal and must not be able to corrupt the
event stream, so the real stdout is duplicated away and fd 1 is pointed at
stderr before any workspace code can run.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import shutil
import sys
import traceback
from pathlib import Path
from typing import Any

ENTRYPOINT = "main.py"


def _open_event_channel():
    """Move the real stdout out of reach, then point fd 1 at stderr."""
    event_fd = os.dup(1)
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    return os.fdopen(event_fd, "w", buffering=1, encoding="utf-8")


def _load_run(ws_path: Path):
    """Import the workspace's main.py by file location.

    Loaded via an explicit spec rather than by putting the workspace on
    sys.path: a workspace directory containing e.g. json.py would otherwise
    shadow the standard library for the rest of this process.
    """
    entry = ws_path / ENTRYPOINT
    spec = importlib.util.spec_from_file_location("datum_workspace_main", entry)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {entry}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    run = getattr(module, "run", None)
    if run is None:
        raise RuntimeError(f"{ENTRYPOINT} has no run()")
    return run


def _store(artifact: dict[str, Any], artifact_dir: Path) -> dict[str, Any]:
    """Land an artifact's bytes in artifact_dir and return its descriptor.

    Content never travels back through the event pipe. A site plan JPEG or a
    multi-megabyte report would otherwise be base64'd through a pipe the parent
    reads line-at-a-time, for no gain: the parent's next move is to write it to
    this same directory.
    """
    name = artifact.get("name")
    if not name:
        raise RuntimeError(f"artifact has no name: {artifact!r}")
    if "/" in name or name.startswith("."):
        raise RuntimeError(f"unsafe artifact name: {name!r}")

    dest = artifact_dir / name
    has_content = "content" in artifact
    has_path = "path" in artifact

    if has_content == has_path:
        raise RuntimeError(f"artifact {name!r} needs exactly one of content/path")

    if has_content:
        content = artifact["content"]
        if isinstance(content, str):
            dest.write_text(content, encoding="utf-8")
        elif isinstance(content, (bytes, bytearray)):
            dest.write_bytes(bytes(content))
        else:
            raise RuntimeError(
                f"artifact {name!r} content must be str or bytes, got "
                f"{type(content).__name__}"
            )
    else:
        src = Path(artifact["path"])
        if not src.is_file():
            raise RuntimeError(f"artifact {name!r} path does not exist: {src}")
        shutil.copyfile(src, dest)

    return {
        "name": name,
        "type": artifact.get("type", "application/octet-stream"),
        "primary": bool(artifact.get("primary", False)),
        "file": name,
        "size": dest.stat().st_size,
    }


async def _main() -> int:
    spec = json.load(sys.stdin)
    ws_path = Path(spec["workspace_path"])
    artifact_dir = Path(spec["artifact_dir"])
    params = spec["params"]
    connections = spec.get("connections", {})

    events = _open_event_channel()

    def send(event: str, **payload: Any) -> None:
        events.write(json.dumps({"event": event, **payload}) + "\n")

    async def emit(event_type: str, payload: dict[str, Any] | None = None) -> None:
        send("emit", type=event_type, payload=payload or {})

    os.chdir(ws_path)

    try:
        run = _load_run(ws_path)
        artifacts = await run(params, emit, connections)
    except Exception:
        send("error", message=traceback.format_exc(limit=20))
        return 1

    if artifacts is None:
        artifacts = []
    if not isinstance(artifacts, list):
        send("error", message=f"run() must return a list, got {type(artifacts).__name__}")
        return 1

    try:
        stored = [_store(a, artifact_dir) for a in artifacts]
    except Exception as e:
        send("error", message=f"{type(e).__name__}: {e}")
        return 1

    send("result", artifacts=stored)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
