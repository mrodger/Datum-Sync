"""REST API and SSE.

The HTTP tests drive the app through an ASGI transport rather than a running
uvicorn: there is no port to collide with and no server to leave behind. The
lifespan is not run by that transport, so the pool is opened here instead.

Nothing here needs the worker. Tests that submit a job assert on the submit
response and the stored row, never on the job reaching a terminal status --
whether a worker happens to be running is not this module's business, and
asserting on it would make the suite pass or fail by coincidence.
"""
from __future__ import annotations

import asyncio
import json
import uuid

import asyncpg
import httpx
import pytest
import pytest_asyncio

# Imported as db_module: the `db` fixture in conftest is a connection, and the
# two names collide.
from datum_sync import config, events, jobs, uploads
from datum_sync import db as db_module
from datum_sync.api import app
from datum_sync.manifest import Manifest


@pytest_asyncio.fixture
async def client(db, token):
    """An authenticated HTTP client bound to the app, sharing the test database.

    Depends on `db` so the repository and job rows these tests create are
    cleaned up by that fixture's teardown, and on `token` so every request
    carries a credential.

    The bearer header is a *default*, not a floor: `client.get(...,
    headers={"authorization": ...})` overrides it, which is how the tests that
    prove the guard works send a bad credential through the same app.
    """
    # The ASGI transport does not run the app's lifespan, so the pool that the
    # routes acquire from has to be opened here.
    await db_module.init_pool()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
        headers={"authorization": f"Bearer {token}"},
    ) as c:
        yield c
    await db_module.close_pool()


# --------------------------------------------------------------------------
# error envelope
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_health_reports_its_dependencies(client):
    r = await client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["database"] == "ok"
    # Either is legitimate; the point is that it is reported, not assumed.
    assert body["worker"] in ("running", "down")


@pytest.mark.asyncio
async def test_unknown_workspace_is_a_not_found_envelope(client):
    r = await client.post(
        "/rest/v1/transformations/submit/_pytest/nosuch", json={"params": {}}
    )
    assert r.status_code == 404
    body = r.json()
    # Every failure carries the same four keys so a client branches on `code`
    # and never on the message text.
    assert set(body) == {"status", "code", "message", "detail"}
    assert body["code"] == "NOT_FOUND"


@pytest.mark.asyncio
async def test_unknown_parameter_is_rejected_at_submit(client, workspace):
    repo, ws = workspace
    r = await client.post(
        f"/rest/v1/transformations/submit/{repo}/{ws}",
        json={"params": {"WHO": "x", "NOT_A_PARAM": 1}},
    )
    assert r.status_code == 400
    assert r.json()["code"] == "INVALID_PARAMETER"


@pytest.mark.asyncio
async def test_missing_required_parameter_is_rejected_at_submit(client, workspace):
    repo, ws = workspace
    r = await client.post(
        f"/rest/v1/transformations/submit/{repo}/{ws}", json={"params": {}}
    )
    assert r.status_code == 400
    assert r.json()["code"] == "INVALID_PARAMETER"


@pytest.mark.asyncio
async def test_malformed_job_id_is_a_bad_request_not_a_crash(client):
    r = await client.get("/rest/v1/transformations/jobs/id/not-a-uuid")
    assert r.status_code == 400
    assert r.json()["code"] == "INVALID_PARAMETER"


@pytest.mark.asyncio
async def test_unknown_job_id_is_not_found(client):
    r = await client.get(f"/rest/v1/transformations/jobs/id/{uuid.uuid4()}")
    assert r.status_code == 404
    assert r.json()["code"] == "NOT_FOUND"


# --------------------------------------------------------------------------
# submit
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_submit_returns_202_with_a_location_header(client, workspace):
    repo, ws = workspace
    r = await client.post(
        f"/rest/v1/transformations/submit/{repo}/{ws}", json={"params": {"WHO": "you"}}
    )
    assert r.status_code == 202
    job_id = r.json()["id"]
    assert r.headers["location"] == f"/rest/v1/transformations/jobs/id/{job_id}"


@pytest.mark.asyncio
async def test_submit_applies_defaults_and_coerces_types(client, workspace, db):
    repo, ws = workspace
    r = await client.post(
        f"/rest/v1/transformations/submit/{repo}/{ws}",
        json={"params": {"WHO": "you"}},
    )
    stored = json.loads(
        await db.fetchval("SELECT params FROM jobs WHERE id = $1",
                          uuid.UUID(r.json()["id"]))
    )
    # LOUD was not supplied; the manifest default is applied at submit so the
    # stored params are the complete set the workspace will actually see.
    assert stored == {"WHO": "you", "LOUD": False}


@pytest.mark.asyncio
async def test_idempotency_key_returns_the_same_job(client, workspace):
    repo, ws = workspace
    headers = {"Idempotency-Key": f"test-{uuid.uuid4()}"}
    body = {"params": {"WHO": "you"}}
    first = await client.post(
        f"/rest/v1/transformations/submit/{repo}/{ws}", json=body, headers=headers
    )
    second = await client.post(
        f"/rest/v1/transformations/submit/{repo}/{ws}", json=body, headers=headers
    )
    assert first.json()["id"] == second.json()["id"]


# --------------------------------------------------------------------------
# the service gate
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_service_path_refuses_a_workspace_that_does_not_publish_it(
    client, workspace
):
    """The fixture manifest declares no services at all.

    Running it anyway would make `services` decorative and a workspace
    author's decision not to expose an endpoint meaningless.
    """
    repo, ws = workspace
    r = await client.get(f"/stream/{repo}/{ws}")
    assert r.status_code == 404
    body = r.json()
    assert body["code"] == "SERVICE_NOT_ENABLED"
    # The caller is told what *is* published rather than left guessing.
    assert "published" in body["detail"]


# --------------------------------------------------------------------------
# uploads: a FILE parameter carries an id, never a path
# --------------------------------------------------------------------------

@pytest.fixture
def upload_store(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_PATH", tmp_path)
    return tmp_path


@pytest.mark.parametrize(
    "given, expected",
    [
        ("../../etc/passwd", "passwd"),
        ("/etc/shadow", "shadow"),
        ("nice name.txt", "nice_name.txt"),
        ("..", "upload"),
        ("", "upload"),
        (None, "upload"),
    ],
)
def test_safe_name_cannot_escape_its_directory(given, expected):
    assert uploads.safe_name(given) == expected


def test_save_then_resolve_round_trips(upload_store):
    upload_id = uploads.save("report.txt", b"hello")
    assert uuid.UUID(upload_id)  # the id is a uuid, so it names nothing else
    assert uploads.resolve(upload_id).read_bytes() == b"hello"


@pytest.mark.parametrize(
    "value",
    ["/etc/passwd", "../../etc/passwd", "", None, 7, "not-a-uuid"],
)
def test_resolve_refuses_anything_that_is_not_an_upload_id(upload_store, value):
    """The property the whole FILE parameter design rests on.

    With this guard removed a caller can name any file the worker can read and
    a workspace that returns its input will hand it straight back.
    """
    with pytest.raises(uploads.UploadError):
        uploads.resolve(value)


def test_resolve_refuses_a_well_formed_but_unknown_id(upload_store):
    with pytest.raises(uploads.UploadError):
        uploads.resolve(str(uuid.uuid4()))


def test_resolve_params_touches_only_file_parameters(upload_store):
    manifest = Manifest.model_validate(
        {
            "name": "m",
            "version": "1.0.0",
            "parameters": [
                {"name": "DOC", "type": "FILE", "required": True},
                {"name": "NOTE", "type": "STRING", "default": "x"},
            ],
            "outputs": [{"name": "out", "type": "text/plain", "primary": True}],
        }
    )
    upload_id = uploads.save("doc.txt", b"body")
    resolved = uploads.resolve_params(
        manifest, {"DOC": upload_id, "NOTE": upload_id}
    )
    assert resolved["DOC"].endswith("doc.txt")
    # NOTE is a STRING: it happens to look like an upload id, and must not be
    # rewritten into a filesystem path because of it.
    assert resolved["NOTE"] == upload_id


# --------------------------------------------------------------------------
# SSE
# --------------------------------------------------------------------------

def _parse(frames: list[str]) -> tuple[list[int], list[str]]:
    """Pull log ids and status values out of raw SSE frames."""
    ids: list[int] = []
    statuses: list[str] = []
    for frame in frames:
        event_id = None
        for line in frame.splitlines():
            if line.startswith("id: "):
                event_id = int(line[4:])
            elif line.startswith("data: "):
                payload = json.loads(line[6:])
                if "status" in payload:
                    statuses.append(payload["status"])
                elif event_id is not None:
                    ids.append(event_id)
    return ids, statuses


@pytest.mark.asyncio
async def test_sse_replay_has_no_gaps_and_no_duplicates(db, workspace):
    """Attach to a job that is logging continuously.

    A reader has to LISTEN before reading job_log, or it loses whatever lands
    in between -- which means the first notifications it sees are usually rows
    the history read also returned. The gap is two round trips, about a
    millisecond, so a workspace logging once a second will almost never put an
    event in it and a reader with no de-duplication passes by luck. Writing
    continuously here makes the window certain to contain events that arrive
    by both routes.
    """
    repo, ws = workspace
    job_id = await jobs.submit(db, repo, ws, {"WHO": "sse"})
    total = 300

    writer = await asyncpg.connect(config.DATABASE_URL, timeout=3)

    async def write_log() -> None:
        try:
            for i in range(total):
                await jobs.log(writer, job_id, f"line {i}")
                await asyncio.sleep(0)
            await jobs.finish(writer, job_id, "complete")
        finally:
            await writer.close()

    reader = await asyncpg.connect(config.DATABASE_URL, timeout=3)
    try:
        task = asyncio.create_task(write_log())
        # Attach while the writer is mid-run, not before it starts: an empty
        # history would leave nothing for de-duplication to get wrong.
        await asyncio.sleep(0.05)
        frames = [
            frame
            async for frame in events.job_events(reader, job_id)
        ]
        await task
    finally:
        await reader.close()

    ids, statuses = _parse(frames)
    assert len(ids) == len(set(ids)), (
        f"{len(ids) - len(set(ids))} duplicated log frames"
    )
    assert sorted(ids) == list(range(min(ids), max(ids) + 1)), "gap in the log ids"
    stored = await db.fetchval(
        "SELECT count(*) FROM job_log WHERE job_id = $1", job_id
    )
    assert len(ids) == stored, f"streamed {len(ids)} of {stored} log rows"
    assert statuses[-1] == "complete"


@pytest.mark.asyncio
async def test_sse_on_a_finished_job_replays_and_closes(db, workspace):
    """A late attach must terminate rather than wait for events that cannot come."""
    repo, ws = workspace
    job_id = await jobs.submit(db, repo, ws, {"WHO": "late"})
    await jobs.log(db, job_id, "only line")
    await jobs.finish(db, job_id, "complete")

    reader = await asyncpg.connect(config.DATABASE_URL, timeout=3)
    try:
        frames = await asyncio.wait_for(
            _collect(events.job_events(reader, job_id)), timeout=5
        )
    finally:
        await reader.close()

    ids, statuses = _parse(frames)
    assert len(ids) == 1
    assert statuses[-1] == "complete"


@pytest.mark.asyncio
async def test_sse_resumes_from_last_event_id(db, workspace):
    """A browser reconnecting sends Last-Event-ID; it must not be re-sent rows."""
    repo, ws = workspace
    job_id = await jobs.submit(db, repo, ws, {"WHO": "resume"})
    for i in range(5):
        await jobs.log(db, job_id, f"line {i}")
    await jobs.finish(db, job_id, "complete")

    seen = await db.fetch(
        "SELECT id FROM job_log WHERE job_id = $1 ORDER BY id", job_id
    )
    cutoff = seen[2]["id"]

    reader = await asyncpg.connect(config.DATABASE_URL, timeout=3)
    try:
        frames = await asyncio.wait_for(
            _collect(events.job_events(reader, job_id, after_id=cutoff)), timeout=5
        )
    finally:
        await reader.close()

    ids, _ = _parse(frames)
    assert ids == [row["id"] for row in seen[3:]]


async def _collect(generator) -> list[str]:
    return [frame async for frame in generator]
