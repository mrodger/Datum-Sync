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

from datum_sync import auth, config, publish, services
from datum_sync.auth import Principal
from datum_sync.manifest import Manifest, ManifestError, load_manifest

ENTRYPOINT = "main.py"
RUN_SIGNATURE = ("params", "emit", "connections")

# Who a CLI sync publishes as when no --as is given.
#
# Unrestricted, and that is not a loophole being left open -- it is a true
# statement about what running this command already requires. Anyone who can run
# it has the database URL and the key that unseals every stored secret, so a
# tier check against them constrains nothing. Pretending otherwise would put a
# lock on a door with no wall attached, and would mean the ordinary `sync` on a
# dev box failed against any tier-2 connection.
#
# The check is real where it binds someone who does *not* already have that:
# `--as NAME` publishes with a named account's max_tier, which is how a
# restricted publisher is tested and how this is meant to be run in anger.
LOCAL_PUBLISHER = Principal(
    account_id=0,
    name="local",
    max_tier=4,
    repo_scope=["*"],
    is_admin=True,
    # None, not the wide-open scope DEV_PRINCIPAL carries. This principal exists
    # to publish workspaces from a shell; it never reaches the MCP vault tools.
    # Granting it the vault would not add capability -- whoever runs this can
    # already read the mount -- it would only mean a publish path that carries
    # vault authority it has no use for.
    vault_scope=None,
    source="local",
)


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
    publisher: str | None = None

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


async def _upsert(
    conn: asyncpg.Connection, report: SyncReport, root: Path, publisher: str
) -> None:
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
                (repository_id, name, description, version, manifest, published_by)
            VALUES ($1, $2, $3, $4, $5, $6)
            ON CONFLICT (repository_id, name) DO UPDATE SET
                description  = EXCLUDED.description,
                version      = EXCLUDED.version,
                manifest     = EXCLUDED.manifest,
                published_at = now(),
                published_by = EXCLUDED.published_by
            """,
            repo_id,
            ws.name,
            ws.manifest.description,
            ws.manifest.version,
            json.dumps(ws.manifest.model_dump(mode="json")),
            publisher,
        )


def _on_disk(root: Path) -> set[tuple[str, str]]:
    """Every workspace directory present, whether or not it loads.

    Deliberately not "every workspace that loaded". Stale means *removed*, and a
    directory that is still there but fails to load has not been removed -- it is
    broken, which is a different fact with a different remedy. Conflating them
    means a typo in manifest.json plus `--prune` deregisters a working published
    workspace, turning a syntax error into an outage.
    """
    return {
        (repo_dir.name, ws_dir.name)
        for repo_dir in _visible_dirs(root)
        for ws_dir in _visible_dirs(repo_dir)
    }


async def _find_stale(
    conn: asyncpg.Connection, root: Path
) -> list[tuple[str, str]]:
    rows = await conn.fetch(
        """
        SELECT r.name AS repository, w.name AS workspace
        FROM workspaces w JOIN repositories r ON r.id = w.repository_id
        """
    )
    on_disk = _on_disk(root)
    return [
        (r["repository"], r["workspace"])
        for r in rows
        if (r["repository"], r["workspace"]) not in on_disk
    ]


async def _apply_gate(
    conn: asyncpg.Connection,
    report: SyncReport,
    publisher: Principal,
    smoke: bool,
) -> None:
    """Run the publish gate over everything that loaded, and drop the failures.

    Dropping from `loaded` is what makes a failed gate mean something: `_upsert`
    only writes what is in that list, so a workspace that fails leaves whatever
    was published before it untouched and still callable. That is the intended
    behaviour -- editing a workspace into an invalid state must not take the
    working version off the air -- and it only holds because this runs *after*
    `_find_stale`, which reads the disk rather than this list.
    """
    passed: list[LoadedWorkspace] = []
    for ws in report.loaded:
        try:
            await publish.gate(
                conn, ws.manifest, ws.path, ws.repository, publisher, smoke=smoke
            )
        except publish.PublishError as e:
            report.errors.append((f"{ws.repository}/{ws.name}", str(e)))
        else:
            passed.append(ws)
    report.loaded = passed


async def sync(
    dry_run: bool = False,
    prune: bool = False,
    as_account: str | None = None,
) -> SyncReport:
    root = config.REPOSITORIES_PATH
    report = scan(root)

    conn = await asyncpg.connect(config.DATABASE_URL)
    try:
        # Before the gate, so that failing the gate is never mistaken for
        # having been deleted. See _on_disk.
        report.stale = await _find_stale(conn, root)

        publisher = (
            await auth.principal_by_name(conn, as_account)
            if as_account
            else LOCAL_PUBLISHER
        )
        report.publisher = publisher.name

        # Smoke tests are skipped on a dry run: --dry-run promises to change
        # nothing, and a workspace's smoke test is arbitrary code that may
        # write to whatever it can reach.
        await _apply_gate(conn, report, publisher, smoke=not dry_run)

        if dry_run:
            return report

        async with conn.transaction():
            await _upsert(conn, report, root, publisher.name)
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
                    # hosted_services has no foreign key to workspaces -- it is
                    # keyed by name for the URL -- so nothing cascades. Left
                    # behind, the row keeps a URL serving a build that can never
                    # be refreshed, because the workspace that made it is gone.
                    await services.deregister_workspace(conn, repo, ws)
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
    who = f" as {report.publisher}" if report.publisher else ""
    print(f"{len(report.loaded)} {mode}{who}, {len(report.errors)} failed.")
    return 0 if report.ok else 1


def main() -> int:
    parser = argparse.ArgumentParser(prog="datum_sync.repository")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("sync", help="reconcile disk with the database")
    p.add_argument("--dry-run", action="store_true", help="report only, change nothing")
    p.add_argument("--prune", action="store_true", help="deregister workspaces not on disk")
    p.add_argument(
        "--as",
        dest="as_account",
        metavar="ACCOUNT",
        help="publish as this service account, enforcing its max_tier "
             "(default: unrestricted local)",
    )
    args = parser.parse_args()

    try:
        report = asyncio.run(
            sync(dry_run=args.dry_run, prune=args.prune, as_account=args.as_account)
        )
    except auth.UnknownAccount as e:
        print(f"  [FAILED ] {e}")
        return 1
    return _print(report, args.dry_run, args.prune)


if __name__ == "__main__":
    sys.exit(main())
