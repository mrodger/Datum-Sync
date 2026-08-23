"""Runner tests: subprocess isolation, event pump, failure paths.

These drive a real subprocess against fixture workspaces on disk. No database
-- Runner does not touch one, and that separation is worth keeping testable.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from datum_sync.manifest import Manifest
from datum_sync.runner import Runner, child_env

MANIFEST = {
    "name": "fixture",
    "version": "1.0.0",
    "outputs": [{"name": "out", "type": "text/plain", "primary": True}],
    "timeout_seconds": 20,
}


def make_ws(tmp_path: Path, body: str, manifest: dict | None = None) -> Path:
    ws = tmp_path / "fixture"
    ws.mkdir(exist_ok=True)
    (ws / "main.py").write_text(body)
    (ws / "manifest.json").write_text(json.dumps(manifest or MANIFEST))
    return ws


async def run_ws(tmp_path: Path, body: str, manifest: dict | None = None,
                 params: dict | None = None):
    ws = make_ws(tmp_path, body, manifest)
    events: list[tuple[str, dict]] = []

    async def sink(event_type, payload):
        events.append((event_type, payload))

    runner = Runner(
        workspace_path=ws,
        manifest=Manifest.model_validate(manifest or MANIFEST),
        params=params or {},
        artifact_dir=tmp_path / "artifacts",
        sink=sink,
    )
    return await runner.run(), events, runner


# -- happy path ------------------------------------------------------------


async def test_run_returns_artifact(tmp_path):
    result, _, _ = await run_ws(tmp_path, """
async def run(params, emit, connections):
    return [{"name": "out", "type": "text/plain", "content": "hello"}]
""")
    assert result.status == "complete"
    assert result.artifacts[0]["size"] == 5
    assert (tmp_path / "artifacts" / "out").read_text() == "hello"


async def test_emit_reaches_the_sink(tmp_path):
    _, events, _ = await run_ws(tmp_path, """
async def run(params, emit, connections):
    await emit("progress", {"pct": 0.5, "message": "half"})
    return []
""")
    assert ("progress", {"pct": 0.5, "message": "half"}) in events


async def test_params_reach_the_workspace(tmp_path):
    result, _, _ = await run_ws(tmp_path, """
async def run(params, emit, connections):
    return [{"name": "out", "type": "text/plain", "content": params["WHO"]}]
""", params={"WHO": "simone"})
    assert (tmp_path / "artifacts" / "out").read_text() == "simone"


async def test_artifact_from_path_is_copied(tmp_path):
    src = tmp_path / "src.txt"
    src.write_text("from disk")
    result, _, _ = await run_ws(tmp_path, f"""
async def run(params, emit, connections):
    return [{{"name": "out", "type": "text/plain", "path": {str(src)!r}}}]
""")
    assert result.status == "complete"
    assert (tmp_path / "artifacts" / "out").read_text() == "from disk"


# -- isolation -------------------------------------------------------------


async def test_workspace_print_does_not_corrupt_the_event_stream(tmp_path):
    """A print() lands in the log; the run still reports its artifact.

    The child duplicates the real stdout away and points fd 1 at stderr before
    importing the workspace. Without that, this line would be parsed as an
    event and the result would be lost.
    """
    result, events, _ = await run_ws(tmp_path, """
print("chatty module-level print")

async def run(params, emit, connections):
    print("and one from inside run")
    return [{"name": "out", "type": "text/plain", "content": "ok"}]
""")
    assert result.status == "complete"
    assert result.artifacts[0]["name"] == "out"
    logged = " ".join(p.get("message", "") for _, p in events)
    assert "chatty module-level print" in logged
    assert "and one from inside run" in logged


async def test_database_url_is_not_in_the_child_environment():
    """A workspace must not be able to reach the control plane's database."""
    env = child_env()
    assert "DATABASE_URL" not in env
    assert not any(k.startswith("DATUM") for k in env)


async def test_workspace_cannot_read_the_control_plane_dsn(tmp_path):
    result, _, _ = await run_ws(tmp_path, """
import os

async def run(params, emit, connections):
    leaked = [k for k in os.environ if "DATABASE" in k or k.startswith("DATUM")]
    return [{"name": "out", "type": "text/plain", "content": repr(leaked)}]
""")
    assert (tmp_path / "artifacts" / "out").read_text() == "[]"


# -- failure paths ---------------------------------------------------------


async def test_exception_fails_the_job_with_a_traceback(tmp_path):
    result, _, _ = await run_ws(tmp_path, """
async def run(params, emit, connections):
    raise ValueError("deliberate")
""")
    assert result.status == "failed"
    assert "ValueError: deliberate" in result.error


async def test_undeclared_artifact_is_rejected(tmp_path):
    result, _, _ = await run_ws(tmp_path, """
async def run(params, emit, connections):
    return [{"name": "surprise", "type": "text/plain", "content": "x"}]
""")
    assert result.status == "failed"
    assert "undeclared" in result.error
    assert "surprise" in result.error


async def test_primary_comes_from_the_manifest_not_the_workspace(tmp_path):
    """A run cannot redesignate its own primary output."""
    result, _, _ = await run_ws(tmp_path, """
async def run(params, emit, connections):
    return [{"name": "out", "type": "application/pdf", "primary": False,
             "content": "x"}]
""")
    assert result.status == "complete"
    assert result.artifacts[0]["primary"] is True
    assert result.artifacts[0]["type"] == "text/plain"


async def test_non_list_return_is_rejected(tmp_path):
    result, _, _ = await run_ws(tmp_path, """
async def run(params, emit, connections):
    return {"name": "out"}
""")
    assert result.status == "failed"
    assert "must return a list" in result.error


async def test_artifact_needs_exactly_one_of_content_or_path(tmp_path):
    result, _, _ = await run_ws(tmp_path, """
async def run(params, emit, connections):
    return [{"name": "out", "type": "text/plain"}]
""")
    assert result.status == "failed"
    assert "content/path" in result.error


async def test_artifact_name_cannot_escape_the_artifact_dir(tmp_path):
    result, _, _ = await run_ws(tmp_path, """
async def run(params, emit, connections):
    return [{"name": "../escaped", "type": "text/plain", "content": "x"}]
""")
    assert result.status == "failed"
    assert not (tmp_path / "escaped").exists()


async def test_missing_path_artifact_is_reported(tmp_path):
    result, _, _ = await run_ws(tmp_path, """
async def run(params, emit, connections):
    return [{"name": "out", "type": "text/plain", "path": "/nonexistent/nope"}]
""")
    assert result.status == "failed"
    assert "does not exist" in result.error


async def test_syntax_error_in_workspace_fails_cleanly(tmp_path):
    result, _, _ = await run_ws(tmp_path, "async def run(params, emit,:\n  pass\n")
    assert result.status == "failed"
    assert "SyntaxError" in result.error


# -- timeout and cancel ----------------------------------------------------


async def test_timeout_kills_the_child(tmp_path):
    manifest = dict(MANIFEST, timeout_seconds=1)
    result, _, runner = await run_ws(tmp_path, """
import asyncio

async def run(params, emit, connections):
    await asyncio.sleep(60)
    return []
""", manifest=manifest)
    assert result.status == "failed"
    assert "timed out after 1s" in result.error
    assert runner._proc.returncode is not None


async def test_timeout_kills_a_child_that_ignores_sigterm(tmp_path):
    """SIGTERM then SIGKILL. A workspace trapping SIGTERM must not hang the pool."""
    manifest = dict(MANIFEST, timeout_seconds=1)
    result, _, runner = await run_ws(tmp_path, """
import asyncio, signal

signal.signal(signal.SIGTERM, lambda *a: None)

async def run(params, emit, connections):
    await asyncio.sleep(60)
    return []
""", manifest=manifest)
    assert result.status == "failed"
    assert runner._proc.returncode is not None


async def test_timeout_kills_processes_the_workspace_spawned(tmp_path):
    """A timeout must reach the grandchildren, not just the Python wrapper.

    SCIMAC workspaces shell out to qgis_process. Signalling only the child
    reaps the wrapper and leaves the render running unsupervised.
    """
    import asyncio
    import os

    pidfile = tmp_path / "grandchild.pid"
    manifest = dict(MANIFEST, timeout_seconds=1)
    await run_ws(tmp_path, f"""
import asyncio, subprocess

async def run(params, emit, connections):
    p = subprocess.Popen(["sleep", "120"])
    open({str(pidfile)!r}, "w").write(str(p.pid))
    await asyncio.sleep(60)
    return []
""", manifest=manifest)

    grandchild = int(pidfile.read_text())
    for _ in range(50):
        try:
            os.kill(grandchild, 0)
        except ProcessLookupError:
            return
        await asyncio.sleep(0.1)
    os.kill(grandchild, 9)
    pytest.fail(f"pid {grandchild} survived the timeout")


async def test_child_does_not_outlive_the_worker(tmp_path):
    """PR_SET_PDEATHSIG: SIGKILL the worker and the child goes with it.

    A driver process starts a Runner and is then SIGKILLed, which is what a
    crashed worker looks like. Before this guard the child survived, and the
    next worker's requeue_orphans() would start a second one alongside it.
    """
    import asyncio
    import json
    import os
    import signal
    import subprocess
    import sys

    from datum_sync import config

    ws = make_ws(tmp_path, """
import asyncio

async def run(params, emit, connections):
    await asyncio.sleep(120)
    return []
""")
    (tmp_path / "m.json").write_text(json.dumps(MANIFEST))
    pidfile = tmp_path / "child.pid"
    driver = tmp_path / "driver.py"
    driver.write_text(
        "import asyncio, json, sys\n"
        "from pathlib import Path\n"
        f"sys.path.insert(0, {str(config.REPO_ROOT)!r})\n"
        "from datum_sync.manifest import Manifest\n"
        "from datum_sync.runner import Runner\n"
        "async def main():\n"
        "    async def sink(t, p): pass\n"
        f"    m = Manifest.model_validate(json.load(open({str(tmp_path / 'm.json')!r})))\n"
        f"    r = Runner(Path({str(ws)!r}), m, {{}}, Path({str(tmp_path / 'art')!r}), sink)\n"
        "    task = asyncio.create_task(r.run())\n"
        "    while r._proc is None:\n"
        "        await asyncio.sleep(0.05)\n"
        f"    open({str(pidfile)!r}, 'w').write(str(r._proc.pid))\n"
        "    await task\n"
        "asyncio.run(main())\n"
    )

    driver_proc = subprocess.Popen([sys.executable, str(driver)])
    try:
        for _ in range(100):
            if pidfile.exists() and pidfile.read_text().strip():
                break
            await asyncio.sleep(0.1)
        else:
            pytest.fail("driver never reported a child pid")

        child_pid = int(pidfile.read_text())
        os.kill(driver_proc.pid, signal.SIGKILL)
        driver_proc.wait()

        for _ in range(60):
            try:
                os.kill(child_pid, 0)
            except ProcessLookupError:
                return
            await asyncio.sleep(0.1)
        os.kill(child_pid, 9)
        pytest.fail(f"child {child_pid} outlived the worker")
    finally:
        if driver_proc.poll() is None:      # pragma: no cover
            driver_proc.kill()


async def test_cancel_stops_a_running_child(tmp_path):
    import asyncio

    ws = make_ws(tmp_path, """
import asyncio

async def run(params, emit, connections):
    await emit("progress", {"pct": 0.1, "message": "working"})
    await asyncio.sleep(60)
    return []
""")
    started = asyncio.Event()

    async def sink(event_type, payload):
        if event_type == "progress":
            started.set()

    runner = Runner(
        workspace_path=ws,
        manifest=Manifest.model_validate(MANIFEST),
        params={},
        artifact_dir=tmp_path / "artifacts",
        sink=sink,
    )
    task = asyncio.create_task(runner.run())
    await asyncio.wait_for(started.wait(), timeout=15)
    await runner.cancel()
    result = await asyncio.wait_for(task, timeout=15)

    assert result.status == "cancelled"
