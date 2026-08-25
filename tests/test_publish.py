"""The publish gate: what must not get published, and what must survive failing.

Almost every test here is a negative one. A gate that lets a good workspace
through is indistinguishable from no gate at all -- the only evidence the gate
exists is a bad workspace being stopped, so that is what is asserted.

Two of them are about something subtler than rejection. A workspace that fails
the gate must leave the previously published version untouched and must not be
counted as removed: editing a workspace into an invalid state should take the
new version off the table, not the working one off the air.
"""
from __future__ import annotations

import json
import textwrap

import pytest
import pytest_asyncio

from datum_sync import connections, crypto, publish, repository
from datum_sync.auth import Principal
from datum_sync.manifest import Manifest

PREFIX = "_pytest-pub"

GOOD_DOC = """\
# fixture

## Purpose
Does a thing.

## Dependencies
Nothing.

## Dependents
The tests in this file.

## Failure modes
It does not.

## Behavioral contracts
No side effects.
"""


def principal(name="pub", max_tier=4) -> Principal:
    return Principal(
        account_id=0, name=name, max_tier=max_tier, repo_scope=None,
        connection_grants=None, is_admin=True, source="local",
    )


def make_ws(tmp_path, name="fixture", doc=GOOD_DOC, main="", **manifest_kw):
    """A workspace directory on disk. Returns its path."""
    ws = tmp_path / name
    ws.mkdir(parents=True, exist_ok=True)
    if doc is not None:
        (ws / publish.DOC_FILE).write_text(doc)
    (ws / "main.py").write_text(main)
    body = {"name": name, "version": "1.0.0", **manifest_kw}
    (ws / "manifest.json").write_text(json.dumps(body))
    return ws


# --------------------------------------------------------------------------
# check 4: the documentation
# --------------------------------------------------------------------------

def test_a_workspace_with_no_manifest_md_is_refused(tmp_path):
    ws = make_ws(tmp_path, doc=None)
    with pytest.raises(publish.PublishError, match="no MANIFEST.md"):
        publish.check_docs(ws)


def test_a_missing_section_is_named(tmp_path):
    doc = GOOD_DOC.replace("## Failure modes\nIt does not.\n", "")
    ws = make_ws(tmp_path, doc=doc)
    with pytest.raises(publish.PublishError, match="Failure modes"):
        publish.check_docs(ws)


def test_the_five_headings_with_nothing_under_them_do_not_pass(tmp_path):
    """The artefact a `Path.exists()` rule produces.

    Someone told to add MANIFEST.md adds MANIFEST.md. If presence is the bar,
    the file that satisfies it is this one, and the check has bought a filename.
    """
    doc = "\n".join(f"## {s}\n" for s in publish.REQUIRED_SECTIONS)
    ws = make_ws(tmp_path, doc=doc)
    with pytest.raises(publish.PublishError, match="empty section"):
        publish.check_docs(ws)


@pytest.mark.parametrize("filler", ["N/A", "n/a", "None", "TBD", "TODO", "-", "..."])
def test_prose_that_says_nothing_counts_as_nothing(tmp_path, filler):
    doc = GOOD_DOC.replace("Nothing.", filler)
    ws = make_ws(tmp_path, doc=doc)
    with pytest.raises(publish.PublishError, match=r"empty section.*Dependencies"):
        publish.check_docs(ws)


def test_a_section_that_is_only_a_comment_counts_as_empty(tmp_path):
    """An HTML comment renders as blank everywhere, so the check agrees."""
    doc = GOOD_DOC.replace("The tests in this file.", "<!-- fill this in later -->")
    ws = make_ws(tmp_path, doc=doc)
    with pytest.raises(publish.PublishError, match="Dependents"):
        publish.check_docs(ws)


def test_headings_are_matched_case_insensitively(tmp_path):
    """"Failure Modes" and "Failure modes" are one section to a reader."""
    doc = GOOD_DOC.replace("## Failure modes", "## Failure Modes")
    make_ws(tmp_path, doc=doc)
    publish.check_docs(tmp_path / "fixture")


def test_a_subheading_stays_inside_its_parent_section(tmp_path):
    """`###` is body, not a new section -- otherwise the parent reads as empty.

    A Failure modes section written as a list of `### case` blocks has no prose
    directly under the `##`, and a splitter that treats every '#' line as a
    heading would reject the most thorough version of the file.
    """
    doc = GOOD_DOC.replace(
        "## Failure modes\nIt does not.\n",
        "## Failure modes\n### Timeout\nSIGKILL.\n",
    )
    make_ws(tmp_path, doc=doc)
    publish.check_docs(tmp_path / "fixture")


def test_a_complete_manifest_md_passes(tmp_path):
    make_ws(tmp_path)
    publish.check_docs(tmp_path / "fixture")


# --------------------------------------------------------------------------
# checks 2 and 3: connections and tier
# --------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def key(monkeypatch):
    monkeypatch.setenv(crypto.KEY_ENV, crypto.generate_key())


@pytest_asyncio.fixture
async def conns(db):
    await db.execute(f"DELETE FROM connections WHERE name LIKE '{PREFIX}%'")
    yield db
    await db.execute(f"DELETE FROM connections WHERE name LIKE '{PREFIX}%'")


async def _make(db, name=f"{PREFIX}-db", **kw):
    kw.setdefault("type_", "database")
    kw.setdefault("config", {"host": "localhost", "database": "x"})
    kw.setdefault("secret", {"password": "hunter2"})
    return await connections.create(db, name, **kw)


def _manifest(**kw) -> Manifest:
    kw.setdefault("name", "fixture")
    kw.setdefault("version", "1.0.0")
    return Manifest(**kw)


@pytest.mark.asyncio
async def test_a_connection_that_does_not_exist_is_refused(conns):
    """Otherwise the workspace publishes and fails on its first run instead."""
    m = _manifest(connections=[{"name": f"{PREFIX}-nope"}])
    with pytest.raises(publish.PublishError, match="does not exist"):
        await publish.check_connections(conns, m, "Testing", principal())


@pytest.mark.asyncio
async def test_a_connection_out_of_scope_is_refused_at_publish_not_at_run(conns):
    """The same predicate resolve() uses, asked a job earlier.

    The gate has every fact needed to know this workspace can never resolve
    that connection. Letting it through means the failure arrives as a runtime
    error in front of whoever submitted the job, who did not choose either.
    """
    await _make(conns, scope="repository", scope_targets=["SCIMAC"])
    m = _manifest(connections=[{"name": f"{PREFIX}-db"}])

    with pytest.raises(publish.PublishError, match="repository-scoped"):
        await publish.check_connections(conns, m, "Testing", principal())

    await publish.check_connections(conns, m, "SCIMAC", principal())


@pytest.mark.asyncio
async def test_the_near_miss_workspace_scope_is_refused(conns):
    """Right repository, wrong workspace. A prefix match would pass this."""
    await _make(conns, scope="workspace", scope_targets=["Testing/other"])
    m = _manifest(connections=[{"name": f"{PREFIX}-db"}])
    with pytest.raises(publish.PublishError, match="workspace-scoped"):
        await publish.check_connections(conns, m, "Testing", principal())


@pytest.mark.asyncio
async def test_declaring_write_on_a_read_only_connection_is_refused(conns):
    await _make(conns, access="read")
    m = _manifest(connections=[{"name": f"{PREFIX}-db", "access": "write"}])
    with pytest.raises(publish.PublishError, match="stored as read-only"):
        await publish.check_connections(conns, m, "Testing", principal())


@pytest.mark.asyncio
async def test_read_on_a_write_connection_is_allowed(conns):
    """The check is one-directional: less access than is stored is fine."""
    await _make(conns, access="write")
    m = _manifest(connections=[{"name": f"{PREFIX}-db"}])
    await publish.check_connections(conns, m, "Testing", principal())


@pytest.mark.asyncio
@pytest.mark.parametrize("tier,max_tier,allowed", [
    (1, 1, True),
    (2, 1, False),
    (4, 3, False),
    (3, 3, True),
    (1, 4, True),
])
async def test_the_publisher_tier_decides(conns, tier, max_tier, allowed):
    """The step-8 deferral, now enforced.

    Checked against the publisher rather than the job's caller because the
    workspace runs with its own authority: whoever publishes it is the person
    choosing to hand that credential to everyone who can submit.
    """
    await _make(conns, tier=tier)
    m = _manifest(connections=[{"name": f"{PREFIX}-db"}])
    p = principal(name="restricted", max_tier=max_tier)

    if allowed:
        await publish.check_connections(conns, m, "Testing", p)
    else:
        with pytest.raises(publish.PublishError, match=f"tier {tier}"):
            await publish.check_connections(conns, m, "Testing", p)


@pytest.mark.asyncio
async def test_a_workspace_declaring_nothing_needs_no_connections(conns):
    await publish.check_connections(conns, _manifest(), "Testing", principal(max_tier=1))


# --------------------------------------------------------------------------
# check 5: the smoke test
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_smoke_test_that_fails_reports_its_own_output(tmp_path):
    """The tail of the output, not just the exit code.

    A gate that says "exited 1" sends the publisher to run the thing by hand to
    find out why, which is the work the gate was supposed to have done.
    """
    ws = make_ws(tmp_path, main=textwrap.dedent("""
        import sys
        print("could not reach the fileshare")
        sys.exit(1)
    """))
    with pytest.raises(publish.PublishError, match="could not reach the fileshare"):
        await publish.run_smoke(ws)


@pytest.mark.asyncio
async def test_a_smoke_test_that_hangs_is_killed_and_fails(tmp_path):
    """Without the kill this is a hung publish -- the same defect as an outage.

    The timeout is passed in short: the point is that the wait ends, and a test
    proving it does should not take SMOKE_TIMEOUT_SECONDS to say so.
    """
    ws = make_ws(tmp_path, main="import time; time.sleep(60)")
    with pytest.raises(publish.PublishError, match="did not finish within"):
        await publish.run_smoke(ws, timeout=2)


@pytest.mark.asyncio
async def test_a_smoke_test_that_passes_passes(tmp_path):
    ws = make_ws(tmp_path, main="print('ok')")
    await publish.run_smoke(ws)


@pytest.mark.asyncio
async def test_the_smoke_test_runs_in_the_workspace_directory(tmp_path):
    """cwd is the workspace, the way the job engine runs it.

    A smoke test that opens a data file next to itself passes here and fails as
    a job if the two disagree about where it is standing.
    """
    ws = make_ws(tmp_path, main=textwrap.dedent("""
        import pathlib, sys
        sys.exit(0 if pathlib.Path("manifest.json").exists() else 1)
    """))
    await publish.run_smoke(ws)


@pytest.mark.asyncio
async def test_the_smoke_test_only_runs_when_the_manifest_asks(conns, tmp_path):
    """Opt-in, because a workspace with no --smoke handler does not fail.

    It runs its normal path with an argument it ignores and exits 0, so an
    always-on check would report "the smoke test passed" for a workspace that
    has none -- while having really run it, side effects and all.
    """
    ws = make_ws(tmp_path, main="import sys; sys.exit(1)")

    m = _manifest(smoke_test=False)
    await publish.gate(conns, m, ws, "Testing", principal(), smoke=True)

    m = _manifest(smoke_test=True)
    with pytest.raises(publish.PublishError, match="exited 1"):
        await publish.gate(conns, m, ws, "Testing", principal(), smoke=True)


@pytest.mark.asyncio
async def test_smoke_false_does_not_spawn_anything(conns, tmp_path):
    """--dry-run promises to change nothing, and a smoke test is arbitrary code."""
    ws = make_ws(tmp_path, main="import sys; sys.exit(1)")
    m = _manifest(smoke_test=True)
    await publish.gate(conns, m, ws, "Testing", principal(), smoke=False)


# --------------------------------------------------------------------------
# the gate, in order
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_the_cheap_checks_run_before_the_expensive_one(conns, tmp_path):
    """A workspace with no MANIFEST.md must not cost a process launch.

    Asserted by making the smoke test destructive: if it ran, the file exists.
    """
    ws = make_ws(tmp_path, doc=None, main=textwrap.dedent("""
        import pathlib
        pathlib.Path("ran").write_text("x")
    """))
    with pytest.raises(publish.PublishError, match="no MANIFEST.md"):
        await publish.gate(conns, _manifest(smoke_test=True), ws, "Testing",
                           principal())
    assert not (ws / "ran").exists()


# --------------------------------------------------------------------------
# what failing the gate must not do
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_failing_the_gate_leaves_the_published_version_alone(db, tmp_path):
    """Editing a workspace into an invalid state takes the *new* version off.

    `_upsert` writes only what is in `report.loaded`, so dropping the failure
    from that list is what makes the previously published row survive. Without
    it a typo in MANIFEST.md deregisters a workspace people are calling.
    """
    repo_id = await db.fetchval(
        "INSERT INTO repositories (name, path) VALUES ($1, $2) RETURNING id",
        "_pytest", str(tmp_path),
    )
    await db.execute(
        "INSERT INTO workspaces (repository_id, name, version, manifest) "
        "VALUES ($1, 'fixture', '1.0.0', '{}')",
        repo_id,
    )

    ws = make_ws(tmp_path, doc=None)
    report = repository.SyncReport(
        loaded=[repository.LoadedWorkspace(
            repository="_pytest", name="fixture", path=ws,
            manifest=_manifest(version="2.0.0"),
        )],
    )
    await repository._apply_gate(db, report, principal(), smoke=False)

    assert report.loaded == []
    assert len(report.errors) == 1
    version = await db.fetchval(
        "SELECT version FROM workspaces WHERE repository_id = $1 AND name = 'fixture'",
        repo_id,
    )
    assert version == "1.0.0"


@pytest.mark.asyncio
async def test_a_workspace_that_fails_the_gate_is_not_stale(db, tmp_path):
    """Stale means *removed*. Broken is a different fact with a different remedy.

    Conflated, a manifest typo plus --prune turns a syntax error into an
    outage -- so `_find_stale` reads the disk rather than the load results.

    The repository is not named `_pytest` here, and cannot be: `_visible_dirs`
    skips names beginning with '_', so a directory called that is invisible to
    the very function under test. It is cleaned up by hand instead.
    """
    name = "pytestpub"
    repo = tmp_path / name
    # No MANIFEST.md: on disk, and would fail the gate.
    make_ws(repo, name="fixture", doc=None)
    (repo / "gone").mkdir()

    try:
        repo_id = await db.fetchval(
            "INSERT INTO repositories (name, path) VALUES ($1, $2) RETURNING id",
            name, str(repo),
        )
        for ws in ("fixture", "gone", "deleted"):
            await db.execute(
                "INSERT INTO workspaces (repository_id, name, version, manifest) "
                "VALUES ($1, $2, '1.0.0', '{}')",
                repo_id, ws,
            )
        (repo / "gone").rmdir()

        stale = await repository._find_stale(db, tmp_path)
        assert (name, "fixture") not in stale
        assert (name, "gone") in stale
        assert (name, "deleted") in stale
    finally:
        await db.execute("DELETE FROM repositories WHERE name = $1", name)
