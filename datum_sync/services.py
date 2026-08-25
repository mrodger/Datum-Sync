"""Hosted services: what a job leaves behind at `/serve/{name}/`.

A hosted service is the one artifact that outlives its job. Everything else a
run produces is fetched by whoever asked for it and forgotten; a `service/*`
output becomes a URL that keeps answering after the job is over.

That difference is why nothing here takes a name from a request. A service is
registered by `register()` on job completion, from an artifact whose *declared*
output type is `service/*` -- so the manifest named it and the publish gate saw
it. A run cannot invent a service the workspace never published, and there is no
route that creates one, because the whole point of the publish gate would be
lost if a job could mint a URL at run time.

Two families, and only one of them works here:

  static      service/static, service/pwa, service/dashboard
              A built directory, served by this process. Fully implemented.

  supervised  service/interactive, service/notebook
              A long-lived subprocess on a fixed port. Not implemented, and
              refused by the publish gate rather than accepted and ignored: a
              workspace that publishes one would otherwise register a URL that
              answers nothing, and be told so only by a visitor.

              Supervision is a different machine -- unit files, health checks,
              restart policy. Running the children under this process instead
              would mean restarting the API kills every hosted application, and
              two API replicas each run their own copy of it.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import asyncpg

from datum_sync import config

STATIC = ("service/static", "service/pwa", "service/dashboard")
SUPERVISED = ("service/interactive", "service/notebook")
TYPES = STATIC + SUPERVISED

INDEX = "index.html"

_COLUMNS = (
    "id, name, type, repository, workspace, source_job, path, command, port, "
    "status, pid, started_at, last_health, updated_at"
)


class ServiceError(Exception):
    """A hosted service cannot be served, and why."""


def is_service(type_: str) -> bool:
    return type_.startswith("service/")


def _served_root(job_id, filename: str) -> Path:
    """The directory a service row will point at, checked before it is stored.

    `filename` comes from a manifest, not a request, and `child._store` already
    refuses a name containing '/'. Checked anyway, because the value written
    here is the root every later `/serve/` request is resolved against: a
    containment bug at write time is a containment bug on every read, and this
    is the last point where there is a job to attribute it to.
    """
    root = config.job_dir(job_id).resolve()
    path = (root / filename).resolve()
    if root not in path.parents:
        raise ServiceError(f"{filename!r} resolves outside the job's artifacts")
    if not path.is_dir():
        raise ServiceError(f"{filename!r} is not a directory")
    return path


# -- registration ------------------------------------------------------------


async def register(
    conn: asyncpg.Connection,
    job_id,
    repository: str,
    workspace: str,
    artifacts: list[dict[str, Any]],
) -> list[str]:
    """Point every service this job produced at this job's artifacts.

    Returns the names registered. Upsert on name, not insert: re-running a
    workspace is how a hosted site is *updated*, and an insert would fail on the
    second run of every workspace that publishes one.

    The swap is atomic without trying to be. `path` moves from one job's
    artifact directory to another's, and the new directory was written in full
    before the job reported complete -- so there is no moment where the URL
    serves a half-copied tree. The old directory is left alone: it is still that
    job's artifacts, and deleting it here would make a completed job's outputs
    disappear because a later run happened to succeed.

    `status` is written 'running', which for a static service is a statement
    about the URL and not about a process: the row existing is what makes
    `/serve/{name}/` answer, so there is no interval in which it is registered
    and not serving. The column earns its other values from the supervised
    family, where a row can exist while the process behind it is down.
    """
    registered: list[str] = []

    for a in artifacts:
        if not is_service(a["type"]):
            continue

        if a["type"] not in STATIC:
            # Unreachable through the gate, which refuses supervised outputs at
            # publish time. Kept because this function writes a row the static
            # server will later trust, and "the caller checked" is not a
            # property this module can see.
            raise ServiceError(
                f"{a['name']!r}: cannot register a {a['type']} service"
            )
        path = str(_served_root(job_id, a["file"]))

        # The WHERE on the DO UPDATE is what stops one workspace taking over
        # another's URL. The publish gate refuses colliding names, so reaching
        # this means two workspaces were published close enough together to race
        # it -- rare, and the wrong thing to resolve by letting the later job
        # win, because the URL is already serving someone else's application.
        claimed = await conn.fetchval(
            """
            INSERT INTO hosted_services
                (name, type, repository, workspace, source_job, path, status)
            VALUES ($1, $2, $3, $4, $5, $6, 'running')
            ON CONFLICT (name) DO UPDATE SET
                type       = EXCLUDED.type,
                source_job = EXCLUDED.source_job,
                path       = EXCLUDED.path,
                updated_at = now()
            WHERE hosted_services.repository = EXCLUDED.repository
              AND hosted_services.workspace = EXCLUDED.workspace
            RETURNING name
            """,
            a["name"], a["type"], repository, workspace, job_id, path,
        )
        if claimed is None:
            # Raised, not logged. A job that reported success while its service
            # silently kept pointing at another workspace is the worst of the
            # three outcomes: the URL is wrong and nothing says so.
            raise ServiceError(
                f"service name {a['name']!r} is already held by another workspace"
            )
        registered.append(a["name"])

    return registered


async def deregister_workspace(
    conn: asyncpg.Connection, repository: str, workspace: str
) -> int:
    """Drop every service a workspace owns. Called when it is pruned.

    A workspace that no longer exists cannot be re-run, so its services can
    never be refreshed again. Leaving the rows would leave URLs serving a build
    nobody can reproduce, from a directory the next job cleanup removes.
    """
    result = await conn.execute(
        "DELETE FROM hosted_services WHERE repository = $1 AND workspace = $2",
        repository, workspace,
    )
    return int(result.split()[-1])


# -- reading -----------------------------------------------------------------


async def get(conn: asyncpg.Connection, name: str) -> asyncpg.Record | None:
    return await conn.fetchrow(
        f"SELECT {_COLUMNS} FROM hosted_services WHERE name = $1", name
    )


async def listing(conn: asyncpg.Connection) -> list[asyncpg.Record]:
    return await conn.fetch(f"SELECT {_COLUMNS} FROM hosted_services ORDER BY name")


# -- serving -----------------------------------------------------------------


def resolve(row: asyncpg.Record, subpath: str) -> Path:
    """The file on disk for `/serve/{name}/{subpath}`.

    Raises ServiceError for anything that is not a readable file inside the
    service root. The traversal check is the whole reason this is a function
    rather than three lines in the route: `subpath` is caller-controlled, the
    root is a real directory, and `Path(root / subpath)` alone happily resolves
    `../../../../etc/passwd`.

    Checked after `resolve()`, not before, and not by inspecting the string for
    "..". A string check is defeated by symlinks and by encodings the router has
    already decoded; comparing resolved absolute paths is defeated by neither.
    """
    if row["type"] in SUPERVISED:
        raise ServiceError(
            f"{row['name']!r} is a {row['type']} service. Supervised services are "
            "registered but not run by this server."
        )
    if row["type"] not in STATIC:
        raise ServiceError(f"{row['name']!r} has unknown type {row['type']!r}")

    root = Path(row["path"]).resolve()
    if not root.is_dir():
        raise ServiceError(
            f"{row['name']!r} points at {row['path']}, which is not a directory; "
            "re-run the workspace to republish it"
        )

    target = (root / subpath).resolve()

    # A directory URL means the index, so that /serve/app/ and /serve/app/docs/
    # both work the way every static host behaves.
    if target.is_dir():
        target = target / INDEX

    if not _within(root, target):
        raise ServiceError("path escapes the service root")
    if not target.is_file():
        raise ServiceError(f"no such file in {row['name']!r}: {subpath or INDEX}")

    return target


def _within(root: Path, target: Path) -> bool:
    """Is `target` inside `root`, both already resolved?

    `is_relative_to`, not `str.startswith`: a root of /data/app and a target of
    /data/app-secrets/key share a prefix as strings and share no directory as
    paths.
    """
    return target == root or target.is_relative_to(root)
