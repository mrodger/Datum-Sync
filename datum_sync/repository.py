"""Repository and workspace loader.

Walks REPOSITORIES_PATH, validates each workspace's interface, and reconciles
what is on disk with the repositories/workspaces tables.

    python -m datum_sync.repository sync             apply
    python -m datum_sync.repository sync --dry-run   report only
    python -m datum_sync.repository sync --prune     also deregister removed

Workspace code is never imported here. The entrypoint is checked by parsing
main.py with ast: the control plane must not execute workspace code, which is
what the job engine's subprocess isolation exists to provide.
"""
from __future__ import annotations

import argparse
import ast
import asyncio
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

import asyncpg

from datum_sync import config
from datum_sync.manifest import Manifest, ManifestError, load_manifest

ENTRYPOINT = "main.py"
RUN_SIGNATURE = ("params", "emit", "connections")


class WorkspaceError(Exception):
    """Workspace on disk does not satisfy the loader's requirements."""


@dataclass
class LoadedWorkspace:
    repository: str
    name: str
    path: Path
    manifest: Manifest


@dataclass
class SyncReport:
    loaded: list[LoadedWorkspace] = field(default_factory=list)
    errors: list[tuple[str, str]] = field(default_factory=list)   # (where, message)
    stale: list[tuple[str, str]] = field(default_factory=list)    # (repo, workspace)

    @property
    def ok(self) -> bool:
        return not self.errors


def check_entrypoint(ws_path: Path) -> None:
    """Verify main.py declares `async def run(params, emit, connections)`.

    Parsed, not imported -- importing would execute module-level workspace code
    inside the control plane.
    """
    entry = ws_path / ENTRYPOINT
    try:
        source = entry.read_text()
    except FileNotFoundError:
        raise WorkspaceError(f"no {ENTRYPOINT}") from None

    try:
        tree = ast.parse(source, filename=str(entry))
    except SyntaxError as e:
        raise WorkspaceError(f"{ENTRYPOINT}: syntax error line {e.lineno}: {e.msg}") from None

    run = next(
        (
            node
            for node in tree.body
            if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef))
            and node.name == "run"
        ),
        None,
    )
    if run is None:
        raise WorkspaceError(f"{ENTRYPOINT}: no module-level `run` function")
    if not isinstance(run, ast.AsyncFunctionDef):
        raise WorkspaceError(f"{ENTRYPOINT}: `run` must be `async def`")

    args = [a.arg for a in run.args.args]
    if tuple(args) != RUN_SIGNATURE:
        raise WorkspaceError(
            f"{ENTRYPOINT}: run{tuple(args)} does not match run{RUN_SIGNATURE}"
        )


def load_workspace(repo_name: str, ws_path: Path) -> LoadedWorkspace:
    manifest = load_manifest(ws_path / "manifest.json", expected_name=ws_path.name)
    check_entrypoint(ws_path)
    return LoadedWorkspace(
        repository=repo_name, name=ws_path.name, path=ws_path, manifest=manifest
    )


def _visible_dirs(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    return sorted(
        p for p in root.iterdir() if p.is_dir() and not p.name.startswith((".", "_"))
    )


def scan(root: Path | None = None) -> SyncReport:
    """Load every workspace under root. Collects errors instead of raising:
    one broken workspace must not hide the rest."""
    root = root or config.REPOSITORIES_PATH
    report = SyncReport()

    for repo_dir in _visible_dirs(root):
        for ws_dir in _visible_dirs(repo_dir):
            where = f"{repo_dir.name}/{ws_dir.name}"
            try:
                report.loaded.append(load_workspace(repo_dir.name, ws_dir))
            except (ManifestError, WorkspaceError) as e:
                report.errors.append((where, str(e)))

    return report


async def _upsert(conn: asyncpg.Connection, report: SyncReport, root: Path) -> None:
    for repo_dir in _visible_dirs(root):
        await conn.execute(
            """
            INSERT INTO repositories (name, path) VALUES ($1, $2)
            ON CONFLICT (name) DO UPDATE SET path = EXCLUDED.path
            """,
            repo_dir.name,
            str(repo_dir),
        )

    for ws in report.loaded:
        repo_id = await conn.fetchval(
            "SELECT id FROM repositories WHERE name = $1", ws.repository
        )
        await conn.execute(
            """
            INSERT INTO workspaces
                (repository_id, name, description, version, manifest)
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (repository_id, name) DO UPDATE SET
                description  = EXCLUDED.description,
                version      = EXCLUDED.version,
                manifest     = EXCLUDED.manifest,
                published_at = now()
            """,
            repo_id,
            ws.name,
            ws.manifest.description,
            ws.manifest.version,
            json.dumps(ws.manifest.model_dump(mode="json")),
        )


async def _find_stale(
    conn: asyncpg.Connection, report: SyncReport
) -> list[tuple[str, str]]:
    rows = await conn.fetch(
        """
        SELECT r.name AS repository, w.name AS workspace
        FROM workspaces w JOIN repositories r ON r.id = w.repository_id
        """
    )
    on_disk = {(w.repository, w.name) for w in report.loaded}
    return [
        (r["repository"], r["workspace"])
        for r in rows
        if (r["repository"], r["workspace"]) not in on_disk
    ]


async def sync(dry_run: bool = False, prune: bool = False) -> SyncReport:
    root = config.REPOSITORIES_PATH
    report = scan(root)

    conn = await asyncpg.connect(config.DATABASE_URL)
    try:
        report.stale = await _find_stale(conn, report)

        if dry_run:
            return report

        async with conn.transaction():
            await _upsert(conn, report, root)
            if prune:
                for repo, ws in report.stale:
                    await conn.execute(
                        """
                        DELETE FROM workspaces
                        WHERE name = $2 AND repository_id =
                            (SELECT id FROM repositories WHERE name = $1)
                        """,
                        repo,
                        ws,
                    )
    finally:
        await conn.close()

    return report


def _print(report: SyncReport, dry_run: bool, prune: bool) -> int:
    for ws in report.loaded:
        print(f"  [ok     ] {ws.repository}/{ws.name}  v{ws.manifest.version}")
    for where, msg in report.errors:
        print(f"  [FAILED ] {where}: {msg}")
    for repo, ws in report.stale:
        verb = "pruned" if (prune and not dry_run) else "stale"
        print(f"  [{verb:7}] {repo}/{ws}: registered but not on disk")

    if report.stale and not prune:
        print("  (run with --prune to deregister stale workspaces)")

    mode = "would register" if dry_run else "registered"
    print(f"{len(report.loaded)} {mode}, {len(report.errors)} failed.")
    return 0 if report.ok else 1


def main() -> int:
    parser = argparse.ArgumentParser(prog="datum_sync.repository")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("sync", help="reconcile disk with the database")
    p.add_argument("--dry-run", action="store_true", help="report only, change nothing")
    p.add_argument("--prune", action="store_true", help="deregister workspaces not on disk")
    args = parser.parse_args()

    report = asyncio.run(sync(dry_run=args.dry_run, prune=args.prune))
    return _print(report, args.dry_run, args.prune)


if __name__ == "__main__":
    sys.exit(main())
