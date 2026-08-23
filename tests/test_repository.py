import json

import pytest

from datum_sync.manifest import ManifestError
from datum_sync.repository import WorkspaceError, check_entrypoint, load_workspace, scan

GOOD_RUN = "async def run(params, emit, connections):\n    return []\n"


def make_ws(root, repo, name, *, source=GOOD_RUN, manifest=None):
    ws = root / repo / name
    ws.mkdir(parents=True)
    (ws / "main.py").write_text(source)
    (ws / "manifest.json").write_text(
        json.dumps(manifest if manifest is not None else {"name": name})
    )
    return ws


# --- entrypoint AST check --------------------------------------------------

def test_valid_entrypoint_accepted(tmp_path):
    check_entrypoint(make_ws(tmp_path, "R", "ws"))


def test_missing_entrypoint_rejected(tmp_path):
    ws = tmp_path / "R" / "ws"
    ws.mkdir(parents=True)
    with pytest.raises(WorkspaceError, match="no main.py"):
        check_entrypoint(ws)


def test_sync_def_run_rejected(tmp_path):
    ws = make_ws(tmp_path, "R", "ws", source="def run(params, emit, connections):\n    pass\n")
    with pytest.raises(WorkspaceError, match="must be `async def`"):
        check_entrypoint(ws)


def test_missing_run_rejected(tmp_path):
    ws = make_ws(tmp_path, "R", "ws", source="async def go(params, emit, connections):\n    pass\n")
    with pytest.raises(WorkspaceError, match="no module-level `run`"):
        check_entrypoint(ws)


def test_wrong_signature_rejected(tmp_path):
    ws = make_ws(tmp_path, "R", "ws", source="async def run(params):\n    pass\n")
    with pytest.raises(WorkspaceError, match="does not match"):
        check_entrypoint(ws)


def test_syntax_error_reported_with_line(tmp_path):
    ws = make_ws(tmp_path, "R", "ws", source="async def run(params, emit, connections:\n")
    with pytest.raises(WorkspaceError, match="syntax error line"):
        check_entrypoint(ws)


def test_entrypoint_is_not_executed(tmp_path):
    """Module-level code must not run during loading."""
    sentinel = tmp_path / "executed.marker"
    source = (
        "from pathlib import Path\n"
        f"Path({str(sentinel)!r}).write_text('boom')\n" + GOOD_RUN
    )
    check_entrypoint(make_ws(tmp_path, "R", "ws", source=source))
    assert not sentinel.exists()


def test_nested_run_does_not_satisfy_the_contract(tmp_path):
    source = "def outer():\n    async def run(params, emit, connections):\n        pass\n"
    ws = make_ws(tmp_path, "R", "ws", source=source)
    with pytest.raises(WorkspaceError, match="no module-level `run`"):
        check_entrypoint(ws)


# --- load_workspace --------------------------------------------------------

def test_manifest_name_must_match_directory(tmp_path):
    ws = make_ws(tmp_path, "R", "ws", manifest={"name": "different"})
    with pytest.raises(ManifestError, match="does not match directory name"):
        load_workspace("R", ws)


# --- scan ------------------------------------------------------------------

def test_scan_collects_errors_without_hiding_good_workspaces(tmp_path):
    make_ws(tmp_path, "R", "good")
    make_ws(tmp_path, "R", "broken", source="def run(params, emit, connections):\n    pass\n")

    report = scan(tmp_path)

    assert [w.name for w in report.loaded] == ["good"]
    assert [where for where, _ in report.errors] == ["R/broken"]
    assert report.ok is False


def test_scan_skips_hidden_and_underscore_dirs(tmp_path):
    make_ws(tmp_path, "R", "real")
    make_ws(tmp_path, "R", "_scratch")
    make_ws(tmp_path, ".git", "objects")

    assert [w.name for w in scan(tmp_path).loaded] == ["real"]


def test_scan_of_missing_root_is_empty_not_an_error(tmp_path):
    report = scan(tmp_path / "nope")
    assert report.loaded == [] and report.ok
