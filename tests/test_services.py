"""Hosted services: a directory artifact, a registered URL, and a static server.

Three layers, tested separately because they fail separately.

`resolve()` is pure and gets the most attention. It is the only code in this
project that takes a caller-supplied path fragment and turns it into a file on
disk, so it is the only place a traversal bug can live -- and the tests for it
use real symlinks and real sibling directories rather than strings that merely
look dangerous, because a string check would pass the string tests and lose to
both of those.

`register()` is tested against the database because the thing worth asserting is
the conditional upsert: re-running a workspace must move its own URL, and must
not be able to move anyone else's.

The route tests are thin on purpose. Everything they could assert about path
handling is already asserted directly against `resolve()`, so what is left is
what only HTTP can show: the credential, the repository scope, and the 409 that
sends a service artifact to `/serve/` instead of returning a directory as a file.
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path

import httpx
import pytest
import pytest_asyncio

from datum_sync import config, publish, services
from datum_sync import db as db_module
from datum_sync.api import app
from datum_sync.auth import Principal
from datum_sync.manifest import Manifest
from datum_sync.runner import Runner

# Every service row these tests create carries this prefix. `hosted_services`
# is keyed by a globally unique name and has no foreign key to the repository,
# so the conftest cleanup cannot reach it -- these tests clean up their own.
PREFIX = "_pytest-svc"


# --------------------------------------------------------------------------
# directory artifacts
# --------------------------------------------------------------------------

SERVICE_MANIFEST = {
    "name": "fixture",
    "version": "1.0.0",
    "outputs": [{"name": "site", "type": "service/static", "primary": True}],
    "timeout_seconds": 20,
}

BUILD_A_TREE = """
import tempfile, pathlib
async def run(params, emit, connections):
    d = pathlib.Path(tempfile.mkdtemp())
    (d / "assets").mkdir()
    (d / "index.html").write_text("<h1>hi</h1>")
    (d / "assets" / "site.css").write_text("h1 {}")
    return [{"name": "site", "type": "service/static", "path": str(d)}]
"""


async def run_ws(tmp_path: Path, body: str, manifest: dict) -> tuple:
    ws = tmp_path / "fixture"
    ws.mkdir(exist_ok=True)
    (ws / "main.py").write_text(body)
    (ws / "manifest.json").write_text(json.dumps(manifest))

    async def sink(event_type, payload):
        pass

    runner = Runner(
        workspace_path=ws,
        manifest=Manifest.model_validate(manifest),
        params={},
        artifact_dir=tmp_path / "artifacts",
        sink=sink,
    )
    return await runner.run(), tmp_path / "artifacts"


async def test_a_directory_artifact_is_copied_whole(tmp_path):
    """Not just the top level. A site that links a stylesheet is the normal case.

    `shutil.copyfile` on a directory raises; `copytree` on one that is missing a
    subdirectory does not, it just quietly produces a smaller tree. So the
    nested file is the assertion, not the directory's existence.
    """
    result, artifacts = await run_ws(tmp_path, BUILD_A_TREE, SERVICE_MANIFEST)

    assert result.status == "complete", result.error
    assert (artifacts / "site" / "index.html").read_text() == "<h1>hi</h1>"
    assert (artifacts / "site" / "assets" / "site.css").read_text() == "h1 {}"


async def test_a_directory_artifact_reports_its_tree_size(tmp_path):
    result, _ = await run_ws(tmp_path, BUILD_A_TREE, SERVICE_MANIFEST)

    art = result.artifacts[0]
    assert art["dir"] is True
    # `dest.stat().st_size` on a directory is the size of the directory entry
    # itself -- typically 4096, and nothing to do with the content. The sum of
    # the two files is 11 + 5.
    assert art["size"] == 16


async def test_a_service_output_returning_a_file_fails(tmp_path):
    """The correspondence check, one way round.

    A `service/static` output that is a single file would register a served root
    that is not a directory, so every request to it 404s -- discovered by a
    visitor rather than by the job that caused it.

    Guard: SERVICE-008.
    """
    result, _ = await run_ws(tmp_path, """
async def run(params, emit, connections):
    return [{"name": "site", "type": "service/static", "content": "not a tree"}]
""", SERVICE_MANIFEST)

    assert result.status == "failed"
    assert "directory" in result.error


async def test_a_directory_under_a_non_service_output_fails(tmp_path):
    """The other way round, and the reason it is checked at all.

    Nothing stops a workspace returning a directory for a `text/plain` output.
    Allowed through, it stores fine and fails later, in `FileResponse`, as a 500
    on the download -- a job that reported success and an artifact that cannot
    be fetched.

    Guard: SERVICE-009.
    """
    result, _ = await run_ws(tmp_path, """
import tempfile
async def run(params, emit, connections):
    return [{"name": "out", "type": "text/plain", "path": tempfile.mkdtemp()}]
""", {
        "name": "fixture",
        "version": "1.0.0",
        "outputs": [{"name": "out", "type": "text/plain", "primary": True}],
        "timeout_seconds": 20,
    })

    assert result.status == "failed"
    assert "directory" in result.error


# --------------------------------------------------------------------------
# resolve: the traversal surface
# --------------------------------------------------------------------------


def site(tmp_path: Path, name: str = "app") -> Path:
    root = tmp_path / name
    (root / "assets").mkdir(parents=True)
    (root / "index.html").write_text("index")
    (root / "assets" / "site.css").write_text("css")
    (root / "assets" / "index.html").write_text("assets index")
    return root


def row(root: Path, type_: str = "service/static", name: str = "app") -> dict:
    # A dict, not a Record: `resolve` only subscripts it, and building a real
    # asyncpg Record would need a database for a function that does not use one.
    return {"name": name, "type": type_, "path": str(root)}


def test_resolve_empty_path_is_the_index(tmp_path):
    root = site(tmp_path)
    assert services.resolve(row(root), "") == root / "index.html"


def test_resolve_finds_a_nested_file(tmp_path):
    root = site(tmp_path)
    assert services.resolve(row(root), "assets/site.css") == root / "assets" / "site.css"


def test_resolve_a_subdirectory_is_its_index(tmp_path):
    """`/serve/app/assets/` behaves the way every static host behaves."""
    root = site(tmp_path)
    assert services.resolve(row(root), "assets") == root / "assets" / "index.html"


def test_resolve_refuses_to_climb_out(tmp_path):
    (tmp_path / "secret.txt").write_text("no")
    root = site(tmp_path)
    with pytest.raises(services.ServiceError, match="escapes"):
        services.resolve(row(root), "../secret.txt")


def test_resolve_refuses_a_symlink_out(tmp_path):
    """The case a string check loses.

    "../" never appears in this request. The escape is on disk, and only
    comparing *resolved* paths can see it.
    """
    (tmp_path / "secret.txt").write_text("no")
    root = site(tmp_path)
    (root / "escape").symlink_to(tmp_path / "secret.txt")

    with pytest.raises(services.ServiceError, match="escapes"):
        services.resolve(row(root), "escape")


def test_resolve_refuses_a_sibling_that_shares_a_prefix(tmp_path):
    """`str.startswith` would allow this one, and it is not contrived.

    /data/app and /data/app-secrets share a prefix as strings and share no
    directory as paths. A served root next to a similarly-named directory is
    exactly what a per-workspace layout produces.

    Guard: SERVICE-001.
    """
    root = site(tmp_path, "app")
    (tmp_path / "app-secrets").mkdir()
    (tmp_path / "app-secrets" / "key").write_text("no")

    with pytest.raises(services.ServiceError, match="escapes"):
        services.resolve(row(root), "../app-secrets/key")


def test_resolve_says_so_when_the_root_is_gone(tmp_path):
    """A row can outlive its directory -- job cleanup does not consult it.

    Distinguished from a missing file because the remedies differ: one is a
    typo in a URL, the other means the workspace must be re-run.
    """
    with pytest.raises(services.ServiceError, match="re-run"):
        services.resolve(row(tmp_path / "never-built"), "")


def test_resolve_missing_file_is_not_the_index(tmp_path):
    root = site(tmp_path)
    with pytest.raises(services.ServiceError, match="no such file"):
        services.resolve(row(root), "nope.html")


def test_resolve_refuses_a_supervised_service(tmp_path):
    """Defence in depth: the publish gate already refuses these.

    Kept because this function decides what to open, and "the gate checked" is
    not a property it can see. A registered supervised row can only exist if the
    gate was bypassed, which is precisely when this needs to hold.

    Guard: SERVICE-003.
    """
    root = site(tmp_path)
    with pytest.raises(services.ServiceError, match="not run by this server"):
        services.resolve(row(root, "service/interactive"), "")


def test_resolve_refuses_an_unknown_type(tmp_path):
    root = site(tmp_path)
    with pytest.raises(services.ServiceError, match="unknown type"):
        services.resolve(row(root, "service/quantum"), "")


# --------------------------------------------------------------------------
# registration
# --------------------------------------------------------------------------


@pytest_asyncio.fixture
async def clean_services(db):
    """Remove every service row these tests create, however they end."""
    yield
    await db.execute("DELETE FROM hosted_services WHERE name LIKE $1", PREFIX + "%")


async def a_job(conn, repository="_pytest", workspace="fixture") -> uuid.UUID:
    """A completed job row with a built site in its artifact directory.

    The repository must be `_pytest` for the conftest cleanup to take the job
    row; the artifact directory is under DATA_PATH and is not cleaned up, which
    is true of every job this server has ever run.
    """
    job_id = await conn.fetchval(
        """
        INSERT INTO jobs (repository, workspace, params, status)
        VALUES ($1, $2, '{}', 'running') RETURNING id
        """,
        repository, workspace,
    )
    root = config.job_dir(job_id) / "site"
    root.mkdir(parents=True, exist_ok=True)
    (root / "index.html").write_text("hello")
    return job_id


def artifact(name: str, type_: str = "service/static") -> dict:
    return {"name": name, "type": type_, "file": "site", "primary": True, "size": 5}


async def test_register_points_at_the_jobs_artifacts(db, clean_services):
    job_id = await a_job(db)
    name = PREFIX + "-basic"

    assert await services.register(
        db, job_id, "_pytest", "fixture", [artifact(name)]) == [name]

    r = await services.get(db, name)
    assert r["path"] == str(config.job_dir(job_id) / "site")
    assert r["source_job"] == job_id
    # 'running' means the URL answers, not that a process exists. A static
    # service is serving from the moment the row is written.
    assert r["status"] == "running"


async def test_register_ignores_non_service_artifacts(db, clean_services):
    job_id = await a_job(db)
    assert await services.register(
        db, job_id, "_pytest", "fixture",
        [{"name": PREFIX + "-nope", "type": "text/plain", "file": "out", "size": 1}],
    ) == []


async def test_rerunning_a_workspace_moves_its_url(db, clean_services):
    """The whole point of the upsert. Re-running is how a hosted site is updated.

    An INSERT would fail on the second run of every workspace that publishes a
    service, which is to say on every deployment after the first.
    """
    name = PREFIX + "-moved"
    first = await a_job(db)
    await services.register(db, first, "_pytest", "fixture", [artifact(name)])

    second = await a_job(db)
    await services.register(db, second, "_pytest", "fixture", [artifact(name)])

    r = await services.get(db, name)
    assert r["source_job"] == second
    assert r["path"] == str(config.job_dir(second) / "site")
    # The old job's artifacts are left alone. Deleting them here would make a
    # completed job's outputs vanish because a later run happened to succeed.
    assert (config.job_dir(first) / "site" / "index.html").is_file()


async def test_another_workspace_cannot_take_the_url(db, clean_services):
    """The `WHERE` on the DO UPDATE, and the raise that makes it visible.

    Without the raise this is worse than a failure: the job reports success, the
    URL keeps serving the first workspace's site, and nothing anywhere says the
    second workspace's output went nowhere.

    Guard: SERVICE-006, SERVICE-007.
    """
    name = PREFIX + "-contested"
    first = await a_job(db)
    await services.register(db, first, "_pytest", "fixture", [artifact(name)])

    second = await a_job(db, workspace="other")
    with pytest.raises(services.ServiceError, match="already held"):
        await services.register(db, second, "_pytest", "other", [artifact(name)])

    assert (await services.get(db, name))["workspace"] == "fixture"


async def test_register_refuses_a_name_that_escapes_the_job(db, clean_services):
    """`file` comes from a manifest, and `child._store` already refuses a '/'.

    Checked again because the value written here is the root every later request
    is resolved against: a containment bug at write time is a containment bug on
    every read, and this is the last point where there is a job to blame.

    Guard: SERVICE-004.
    """
    job_id = await a_job(db)
    bad = artifact(PREFIX + "-escape") | {"file": "../../../etc"}
    with pytest.raises(services.ServiceError, match="outside"):
        await services.register(db, job_id, "_pytest", "fixture", [bad])


async def test_register_refuses_a_file(db, clean_services):
    # Guard: SERVICE-005.
    job_id = await a_job(db)
    (config.job_dir(job_id) / "flat").write_text("x")
    bad = artifact(PREFIX + "-flat") | {"file": "flat"}
    with pytest.raises(services.ServiceError, match="not a directory"):
        await services.register(db, job_id, "_pytest", "fixture", [bad])


async def test_register_refuses_a_supervised_service(db, clean_services):
    """Unreachable through the gate. Enforced here anyway.

    This function writes a row the static server will later trust, and it cannot
    see whether its caller checked.
    """
    job_id = await a_job(db)
    with pytest.raises(services.ServiceError, match="cannot register"):
        await services.register(
            db, job_id, "_pytest", "fixture",
            [artifact(PREFIX + "-live", "service/notebook")])


async def test_deregister_drops_only_that_workspaces_services(db, clean_services):
    mine, theirs = PREFIX + "-mine", PREFIX + "-theirs"
    a = await a_job(db)
    await services.register(db, a, "_pytest", "fixture", [artifact(mine)])
    b = await a_job(db, workspace="other")
    await services.register(db, b, "_pytest", "other", [artifact(theirs)])

    assert await services.deregister_workspace(db, "_pytest", "fixture") == 1
    assert await services.get(db, mine) is None
    assert await services.get(db, theirs) is not None


# --------------------------------------------------------------------------
# the publish gate
# --------------------------------------------------------------------------


def manifest_with(output_type: str, name: str = "site") -> Manifest:
    return Manifest.model_validate({
        "name": "fixture",
        "version": "1.0.0",
        "outputs": [{"name": name, "type": output_type, "primary": True}],
    })


async def test_gate_refuses_a_supervised_output(db):
    """Refused at publish, not accepted and quietly ignored.

    Accepted, it registers a URL that answers nothing, and the workspace author
    is told so by a visitor. Refused, the author is told by `sync`.

    Guard: SERVICE-010.
    """
    with pytest.raises(publish.PublishError, match="not run by this server"):
        await publish.check_services(db, manifest_with("service/notebook"), "_pytest")


async def test_gate_allows_the_static_family(db):
    for t in services.STATIC:
        await publish.check_services(db, manifest_with(t), "_pytest")


async def test_gate_refuses_a_name_another_workspace_serves(db, clean_services):
    """The service namespace is global, because the URL namespace is global.

    Caught at publish because that is the only moment where refusing is
    actionable: at job completion, the choice is between silently changing an
    owner and failing a run that did its work.

    Guard: SERVICE-011.
    """
    name = PREFIX + "-taken"
    job_id = await a_job(db)
    await services.register(db, job_id, "_pytest", "fixture", [artifact(name)])

    m = manifest_with("service/static", name)
    m.name = "another"
    with pytest.raises(publish.PublishError, match="already served by"):
        await publish.check_services(db, m, "_pytest")


async def test_gate_allows_a_workspace_to_republish_its_own(db, clean_services):
    """Otherwise the second `sync` after publishing a service always fails."""
    name = PREFIX + "-own"
    job_id = await a_job(db)
    await services.register(db, job_id, "_pytest", "fixture", [artifact(name)])

    await publish.check_services(db, manifest_with("service/static", name), "_pytest")


# --------------------------------------------------------------------------
# the route
# --------------------------------------------------------------------------


@pytest_asyncio.fixture
async def client(db, token):
    await db_module.init_pool()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
        headers={"authorization": f"Bearer {token}"},
    ) as c:
        yield c
    await db_module.close_pool()


@pytest_asyncio.fixture
async def served(db, clean_services):
    """A registered service with a real two-file tree behind it."""
    name = PREFIX + "-live"
    job_id = await a_job(db)
    root = config.job_dir(job_id) / "site"
    (root / "assets").mkdir(exist_ok=True)
    (root / "assets" / "site.css").write_text("h1 {}")
    await services.register(db, job_id, "_pytest", "fixture", [artifact(name)])
    return name


async def test_serve_returns_the_index(client, served):
    r = await client.get(f"/serve/{served}/")
    assert r.status_code == 200
    assert r.text == "hello"


async def test_serve_returns_a_nested_file(client, served):
    r = await client.get(f"/serve/{served}/assets/site.css")
    assert r.status_code == 200
    assert r.text == "h1 {}"


async def test_serve_refuses_traversal(client, served):
    """Percent-encoded, and that is not a flourish -- it is the only reachable form.

    Measured: httpx applies RFC 3986 dot-segment removal when it builds the
    request, so `/serve/x/../../etc/passwd` leaves the client as `/etc/passwd`
    and never reaches this route at all. A test written that way asserts on
    somebody else's 404. The encoded form arrives intact, is decoded by the
    router into `../../..`, and lands in `subpath` -- which is also the form a
    real attacker would send, for exactly the reason it survives here.

    The error code is asserted as well as the status, because 404 is what an
    unknown service name returns too, and that would pass with the guard gone.

    Guard: SERVICE-002.
    """
    escape = "%2e%2e%2f%2e%2e%2f%2e%2e%2f%2e%2e%2fetc%2fpasswd"
    r = await client.get(f"/serve/{served}/{escape}")
    assert r.status_code == 404
    assert r.json()["code"] == "SERVICE_UNAVAILABLE"
    assert "escapes" in r.json()["message"]


async def test_serve_unknown_name_is_404(client):
    r = await client.get("/serve/no-such-service/")
    assert r.status_code == 404
    assert r.json()["code"] == "NOT_FOUND"


async def test_serve_needs_a_credential(client, served):
    """Not public. A built site is built from a repository's data.

    Making the whole namespace world-readable because static files feel harmless
    would publish whatever the last job happened to write.

    Guard: SERVICE-014.
    """
    r = await client.get(f"/serve/{served}/", headers={"authorization": ""})
    assert r.status_code == 401


async def test_serve_checks_the_owning_repositorys_scope(client, db, served):
    """The URL does not mention a repository. The scope check still applies.

    Otherwise a caller confined to one repository reads another's site through a
    name that gives no hint of where it came from.

    Guard: SERVICE-012.
    """
    await db.execute(
        "UPDATE service_accounts SET repo_scope = $2 WHERE name = $1",
        "_pytest", ["SomethingElse"],
    )
    r = await client.get(f"/serve/{served}/")
    assert r.status_code == 403


async def test_listing_is_filtered_by_scope(client, db, served):
    """Membership, not equality -- and the first version got that wrong.

    A hosted service outlives its job on purpose, so any database that has ever
    run the site fixture keeps a row for it. Asserting the whole list made this
    test pass only on a machine where the feature had never been used, and it
    duly failed the first time the end-to-end check was run before the suite.

    Guard: SERVICE-013.
    """
    async def visible() -> list[str]:
        r = await client.get("/rest/v1/services")
        return [s["name"] for s in r.json()["items"]]

    assert served in await visible()

    await db.execute(
        "UPDATE service_accounts SET repo_scope = $2 WHERE name = $1",
        "_pytest", ["SomethingElse"],
    )
    assert served not in await visible()


async def test_a_service_artifact_is_not_downloadable(client, db, served):
    """It is a directory. `FileResponse` on one is a 500 at the transport layer.

    409 with the URL, rather than 404: the artifact exists and is reachable, just
    not through a route whose contract is a single file.

    Guard: SERVICE-015.
    """
    job_id = await db.fetchval(
        "SELECT source_job FROM hosted_services WHERE name = $1", served)
    await db.execute(
        "UPDATE jobs SET status = 'complete', artifacts = $2 WHERE id = $1",
        job_id, json.dumps([artifact(served)]),
    )

    r = await client.get(
        f"/rest/v1/transformations/jobs/id/{job_id}/artifacts/{served}")
    assert r.status_code == 409
    assert f"/serve/{served}/" in r.json()["message"]
