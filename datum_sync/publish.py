"""The publish gate: may this workspace be registered, and by whom.

`repository.py` answers a structural question -- is there a directory with a
manifest that parses and a `run` with the right signature. This module answers a
different one, and the difference is the point of the gate: a workspace can be
perfectly well-formed and still not be publishable, because the credentials it
declares do not exist, or the person publishing it is not allowed to hand them
out.

Five checks, in the order the spec fixes them:

  1. manifest.json validates          -- done by `manifest.load_manifest`
  2. declared connections exist       -- `check_connections`
  3. publisher's max_tier is enough   -- `check_connections`
  4. MANIFEST.md is filled in         -- `check_docs`
  5. optional smoke test exits 0      -- `run_smoke`

plus one the contract implies rather than lists: a `service/*` output claims a
global URL, so `check_services` refuses a name another workspace already serves,
and a service type this server cannot run.

They run cheapest-first. Check 5 spawns a subprocess, so nothing reaches it
until the free checks have passed: a workspace with a broken manifest should
never cost a process launch.

Every check raises `PublishError` with a message naming the workspace and the
remedy. They do not return a bool -- a gate that reports failure as a value gets
called in a boolean context and inverted by accident, whereas an exception that
is never caught is loud.

The gate deliberately does not import or execute workspace code, for the same
reason `repository.check_entrypoint` parses rather than imports. The smoke test
is the one exception, and it runs in a subprocess with a timeout precisely
because it *is* an exception.
"""
from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path

import asyncpg

from datum_sync import connections, services
from datum_sync.auth import Principal
from datum_sync.manifest import Manifest

DOC_FILE = "MANIFEST.md"

# Fixed by spec/workspace-contract.md. Not configurable: the value of the file
# is that every workspace answers the same five questions, and a per-workspace
# override is a way of not answering one.
REQUIRED_SECTIONS = (
    "Purpose",
    "Dependencies",
    "Dependents",
    "Failure modes",
    "Behavioral contracts",
)

SMOKE_TIMEOUT_SECONDS = 30

# Prose that is present but says nothing. A section containing only this is
# treated as empty, because the check exists to make someone think about the
# question, and "N/A" is the shape of not having.
_EMPTY_PROSE = re.compile(
    r"^(n/?a|none|tbd|todo|xxx+|-+|\.\.\.)[.!]?$", re.IGNORECASE
)


class PublishError(Exception):
    """A workspace that is well-formed but must not be published."""


# --- check 4: documentation -------------------------------------------------


def _sections(text: str) -> dict[str, str]:
    """Split a MANIFEST.md into `## Heading` -> body.

    Only `##` is a section. A `###` inside Failure modes is part of that
    section's body, which is why this does not simply split on lines starting
    with '#'.
    """
    found: dict[str, str] = {}
    current: str | None = None
    body: list[str] = []

    for line in text.splitlines():
        m = re.match(r"^##\s+(.*?)\s*$", line)
        if m and not line.startswith("###"):
            if current is not None:
                found[current] = "\n".join(body)
            current = m.group(1)
            body = []
        elif current is not None:
            body.append(line)

    if current is not None:
        found[current] = "\n".join(body)
    return found


def _is_empty(body: str) -> bool:
    """Does this section body actually say anything?

    Comments are stripped first: an HTML comment is invisible in every renderer,
    so a section that is only a comment reads as blank to every human who opens
    the file, and the check should agree with them.
    """
    stripped = re.sub(r"<!--.*?-->", "", body, flags=re.DOTALL)
    lines = [ln.strip() for ln in stripped.splitlines()]
    content = [ln for ln in lines if ln and not _EMPTY_PROSE.match(ln)]
    return not content


def check_docs(ws_path: Path) -> None:
    """Check 4: MANIFEST.md exists and every required section has content.

    Presence alone is not the bar. A file with the five headings and nothing
    under them passes a `Path.exists()` check while documenting nothing, and
    that is the exact artefact a required-file rule produces if it only checks
    for the file.
    """
    doc = ws_path / DOC_FILE
    try:
        text = doc.read_text()
    except FileNotFoundError:
        raise PublishError(
            f"no {DOC_FILE}; every workspace must document itself "
            f"(sections: {', '.join(REQUIRED_SECTIONS)})"
        ) from None

    found = _sections(text)
    # Compared case-insensitively: "Failure Modes" and "Failure modes" are the
    # same section to a reader, so they are the same section here.
    lowered = {k.lower(): v for k, v in found.items()}

    missing = [s for s in REQUIRED_SECTIONS if s.lower() not in lowered]
    if missing:
        raise PublishError(f"{DOC_FILE} is missing section(s): {missing}")

    blank = [s for s in REQUIRED_SECTIONS if _is_empty(lowered[s.lower()])]
    if blank:
        raise PublishError(f"{DOC_FILE} has empty section(s): {blank}")


# --- checks 2 and 3: connections and tier -----------------------------------


async def check_connections(
    conn: asyncpg.Connection,
    manifest: Manifest,
    repository: str,
    publisher: Principal,
) -> None:
    """Checks 2 and 3: every declared connection exists, is in scope, and is
    within the publisher's tier.

    Publishing is the moment a workspace stops being a directory and becomes
    something anyone with submit rights can run. Whatever credentials it
    declares, it will use with *its own* authority from then on and not the
    caller's -- so the tier is checked against whoever publishes it, once, here.
    Checking it at run time instead would ask the wrong person: the caller
    submitting a job never chose which connections the workspace uses.

    Scope is checked with the same predicate `connections.resolve` uses at run
    time. A workspace that publishes and then fails on every run because its
    connection is scoped to a different repository has been let through a gate
    that had the information to stop it.
    """
    for ref in manifest.connections:
        row = await connections.get(conn, ref.name)
        if row is None:
            raise PublishError(
                f"declares connection {ref.name!r}, which does not exist in the "
                "connection store"
            )

        # workspace-scoped connections name repository/workspace, so the
        # manifest's own name is the workspace being published.
        if not connections.matches_scope(row, repository, manifest.name):
            raise PublishError(
                f"connection {ref.name!r} is {row['scope']}-scoped and does not "
                f"cover {repository}/{manifest.name}"
            )

        # A workspace declaring write on a read-only connection would fail at
        # run time, inside its own code, with whatever error the target gives
        # for a refused write. Cheaper to say so now.
        if ref.access == "write" and row["access"] != "write":
            raise PublishError(
                f"declares write access to connection {ref.name!r}, which is "
                f"stored as {row['access']}-only"
            )

        if row["tier"] > publisher.max_tier:
            raise PublishError(
                f"connection {ref.name!r} is tier {row['tier']} and "
                f"{publisher.name!r} may publish up to tier "
                f"{publisher.max_tier}"
            )


# --- hosted services --------------------------------------------------------


async def check_services(
    conn: asyncpg.Connection, manifest: Manifest, repository: str
) -> None:
    """A `service/*` output must name a supported type and a free URL.

    Publishing is the right moment for both, and the only one. `/serve/{name}/`
    has nothing in it but the name, so the namespace is global, and the
    alternative to checking here is discovering the clash when a job completes
    -- at which point either the URL silently changes owner or a run that did
    its work is reported as failed. Neither is something the person who wrote
    the workspace can act on; a refused publish is.
    """
    for out in manifest.outputs:
        if not services.is_service(out.type):
            continue

        if out.type in services.SUPERVISED:
            raise PublishError(
                f"output {out.name!r} is {out.type}; supervised services are not "
                "run by this server (static, pwa and dashboard are)"
            )
        if out.type not in services.STATIC:
            raise PublishError(
                f"output {out.name!r} has unknown service type {out.type!r}; "
                f"valid: {', '.join(services.STATIC)}"
            )

        owner = await conn.fetchrow(
            "SELECT repository, workspace FROM hosted_services WHERE name = $1",
            out.name,
        )
        if owner is not None and (
            owner["repository"] != repository or owner["workspace"] != manifest.name
        ):
            raise PublishError(
                f"service name {out.name!r} is already served by "
                f"{owner['repository']}/{owner['workspace']}"
            )


# --- check 5: smoke test ----------------------------------------------------


async def run_smoke(ws_path: Path, timeout: int = SMOKE_TIMEOUT_SECONDS) -> None:
    """Check 5: `python main.py --smoke` exits 0.

    This is the one place the gate runs workspace code, so it runs it the way
    the job engine does -- a subprocess, cwd set to the workspace, with a
    timeout it cannot talk its way out of. A smoke test that hangs is a failed
    smoke test; without the kill it would be a hung publish instead, which is
    the same defect reported as an outage.

    sys.executable, not "python": the gate must use the interpreter datum-sync
    is running under, or the smoke test runs against a different set of
    installed packages than the job ever will.
    """
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "main.py",
        "--smoke",
        cwd=str(ws_path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        # kill, not terminate: this is already the unco-operative case, and a
        # SIGTERM a hung process ignores leaves it parented to the publish.
        proc.kill()
        await proc.wait()
        raise PublishError(
            f"smoke test did not finish within {timeout}s"
        ) from None

    if proc.returncode != 0:
        tail = (out or b"").decode(errors="replace").strip().splitlines()[-5:]
        detail = ("; ".join(tail))[:500] if tail else "no output"
        raise PublishError(
            f"smoke test exited {proc.returncode}: {detail}"
        )


# --- the gate ---------------------------------------------------------------


async def gate(
    conn: asyncpg.Connection,
    manifest: Manifest,
    ws_path: Path,
    repository: str,
    publisher: Principal,
    smoke: bool = True,
) -> None:
    """Run every check that applies. Raises PublishError on the first failure.

    First failure, not all failures: the checks are ordered by cost, and
    collecting them all would mean running a smoke test for a workspace whose
    connections do not exist.

    `smoke=False` exists for callers that must not spawn processes -- the
    dry-run path, which promises to change nothing and where a smoke test that
    writes to a database would break that promise.
    """
    check_docs(ws_path)
    await check_services(conn, manifest, repository)
    await check_connections(conn, manifest, repository, publisher)
    if smoke and manifest.smoke_test:
        await run_smoke(ws_path)
