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
    # Every failure carries the same five keys so a client branches on `code`
    # and never on the message text.
    assert set(body) == {"status", "code", "message", "detail", "trace_id"}
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
# the flat workspace catalogue
# --------------------------------------------------------------------------

async def _catalogue_row(client, repo, ws):
    """The matching rows, as a list.

    A list rather than a `next()`, because the failure these tests exist to
    catch is the row being absent -- and inside an async test `next()` raises
    StopIteration, which asyncio re-raises as `RuntimeError: coroutine raised
    StopIteration` from base_events.py. The break test still failed, but it
    named the event loop instead of the missing workspace.
    """
    body = (await client.get("/rest/v1/workspaces")).json()
    return [w for w in body["items"] if w["repository"] == repo and w["name"] == ws]


@pytest.mark.asyncio
async def test_flat_catalogue_lists_a_workspace_that_has_never_run(client, workspace):
    """A workspace with no jobs must still appear.

    The aggregate is a LEFT JOIN for exactly this reason: an inner join gives
    the same row count on a database where everything has run at least once,
    and silently drops every newly published workspace -- the ones a catalogue
    most needs to show. `jobs == 0` is the assertion that tells the two apart.
    """
    repo, ws = workspace
    rows = await _catalogue_row(client, repo, ws)
    assert len(rows) == 1, "the workspace is missing from the flat catalogue"
    assert rows[0]["jobs"] == 0
    assert rows[0]["last_run"] is None
    assert rows[0]["version"] == "1.0.0"


@pytest.mark.asyncio
async def test_flat_catalogue_counts_jobs_per_workspace(client, workspace):
    repo, ws = workspace
    for _ in range(2):
        r = await client.post(
            f"/rest/v1/transformations/submit/{repo}/{ws}",
            json={"params": {"WHO": "you"}},
        )
        assert r.status_code == 202

    rows = await _catalogue_row(client, repo, ws)
    assert len(rows) == 1, "the workspace is missing from the flat catalogue"
    assert rows[0]["jobs"] == 2
    assert rows[0]["last_run"] is not None


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
async def test_every_status_frame_carries_the_same_fields(db, workspace):
    """A subscriber watches the stream so it does not have to poll the job.

    That only works if a status frame says everything the job row would. The
    non-terminal frame used to be built from the NOTIFY payload instead of the
    row, so `error`, `artifacts`, `started_at` and `completed_at` were present
    on the last frame and absent from the ones before it -- one event name with
    two shapes. The UI showed the consequence: a job reading COMPLETE with no
    finish time, because it had rendered the timestamps once and had nothing to
    update them from.

    Looking for the `running` frame turned up the larger half: there was no
    such frame. `claim` set the status and started_at and announced neither, so
    a watcher saw QUEUED for the whole run and then COMPLETE.

    Guard: API-001, API-002.
    """
    repo, ws = workspace
    job_id = await jobs.submit(db, repo, ws, {"WHO": "shape"})

    writer = await asyncpg.connect(config.DATABASE_URL, timeout=3)

    async def drive() -> None:
        try:
            await asyncio.sleep(0.05)
            # claim() takes the oldest queued job, not a named one. The suite
            # runs with no worker and the fixture cleans up after each test, so
            # the only queued job is this one -- asserted rather than assumed.
            claimed = await jobs.claim(writer)
            assert claimed is not None and claimed["id"] == job_id
            await asyncio.sleep(0.05)
            await jobs.finish(writer, job_id, "complete")
        finally:
            await writer.close()

    reader = await asyncpg.connect(config.DATABASE_URL, timeout=3)
    try:
        task = asyncio.create_task(drive())
        frames = await asyncio.wait_for(
            _collect(events.job_events(reader, job_id)), timeout=5
        )
        await task
    finally:
        await reader.close()

    payloads = [
        json.loads(line[6:])
        for frame in frames
        for line in frame.splitlines()
        if line.startswith("data: ") and "status" in json.loads(line[6:])
    ]
    expected = {"job_id", "status", "error", "artifacts",
                "started_at", "completed_at"}
    assert len(payloads) >= 2, "expected a frame before the terminal one"
    for payload in payloads:
        assert set(payload) == expected, payload

    assert payloads[-1]["status"] == "complete"
    assert payloads[-1]["completed_at"] is not None
    # The frame that reported `running` already knew when it started, which is
    # the field a watcher cannot obtain any other way.
    running = [p for p in payloads if p["status"] == "running"]
    assert running and running[-1]["started_at"] is not None


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


# --------------------------------------------------------------------------
# the job queue listing
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_the_job_listing_returns_a_submitted_job(client, workspace):
    repo, ws = workspace
    submit = await client.post(
        f"/rest/v1/transformations/submit/{repo}/{ws}", json={"params": {"WHO": "x"}}
    )
    job_id = submit.json()["id"]

    r = await client.get("/rest/v1/transformations/jobs")
    assert r.status_code == 200
    listed = {j["id"]: j for j in r.json()["items"]}
    assert job_id in listed
    assert listed[job_id]["workspace"] == ws
    assert listed[job_id]["artifact_count"] == 0
    # The summary deliberately omits params: the queue table does not show them
    # and they are unbounded in size.
    assert "params" not in listed[job_id]


@pytest.mark.asyncio
async def test_the_job_listing_filters_by_status(client, workspace):
    repo, ws = workspace
    submit = await client.post(
        f"/rest/v1/transformations/submit/{repo}/{ws}", json={"params": {"WHO": "x"}}
    )
    job_id = submit.json()["id"]

    queued = await client.get("/rest/v1/transformations/jobs?status=queued")
    assert job_id in {j["id"] for j in queued.json()["items"]}

    done = await client.get("/rest/v1/transformations/jobs?status=complete")
    assert job_id not in {j["id"] for j in done.json()["items"]}


@pytest.mark.asyncio
async def test_the_job_listing_rejects_an_unknown_status(client):
    """Rather than silently returning everything, which is what `WHERE status =
    $1` with an unmatched value looks like from the outside: an empty list."""
    r = await client.get("/rest/v1/transformations/jobs?status=finished")
    assert r.status_code == 400
    assert r.json()["code"] == "INVALID_PARAMETER"
    assert "queued" in r.json()["detail"]["valid"]


# --------------------------------------------------------------------------
# the job summary the dashboard counts from
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_the_summary_names_every_status_even_at_zero(client):
    """The dashboard draws one tile per status. If a status vanished from the
    response whenever nothing was in it, the row would change width as the
    queue emptied -- and `counts[status]` would be undefined, which renders as
    the word "undefined" rather than as a number."""
    r = await client.get("/rest/v1/transformations/jobs/summary")
    assert r.status_code == 200
    assert set(r.json()["counts"]) == {
        "queued", "running", "complete", "failed", "cancelled"
    }


@pytest.mark.asyncio
async def test_the_summary_counts_a_submitted_job(client, workspace):
    repo, ws = workspace
    before = (await client.get("/rest/v1/transformations/jobs/summary")).json()

    await client.post(
        f"/rest/v1/transformations/submit/{repo}/{ws}", json={"params": {"WHO": "x"}}
    )

    after = (await client.get("/rest/v1/transformations/jobs/summary")).json()
    # Relative, not absolute: a worker may be running alongside the suite and
    # move this job out of `queued` between the submit and the read. What
    # cannot happen is the total staying put.
    assert after["total"] == before["total"] + 1


@pytest.mark.asyncio
async def test_the_summary_counts_past_the_listing_limit(client, workspace, db):
    """The reason this endpoint exists rather than the dashboard counting the
    job list. That list is capped at 500 rows and reports `len(items)`, so on a
    queue larger than the cap it would report the cap -- a wrong number,
    arrived at silently, on the first screen anybody sees.

    Proven at a smaller scale than 500 by asking the list for one row: the
    summary must not agree with it.
    """
    repo, ws = workspace
    for _ in range(3):
        await client.post(
            f"/rest/v1/transformations/submit/{repo}/{ws}",
            json={"params": {"WHO": "x"}},
        )

    page = (await client.get("/rest/v1/transformations/jobs?limit=1")).json()
    summary = (await client.get("/rest/v1/transformations/jobs/summary")).json()

    assert page["count"] == 1
    assert summary["total"] >= 3


@pytest.mark.asyncio
async def test_a_scoped_caller_is_not_told_how_busy_the_others_are(
    client, workspace, scoped_token
):
    """A count is a smaller leak than a row, not a different one. A caller
    confined to one repository must not learn the queue depth of the ones it
    cannot see -- and the job list already refuses to tell them, so an
    unfiltered summary next to it would be a hole with a filter beside it.

    Guard: API-003.
    """
    repo, ws = workspace
    await client.post(
        f"/rest/v1/transformations/submit/{repo}/{ws}", json={"params": {"WHO": "x"}}
    )

    # Scoped to 'Elsewhere/*', which is not the fixture's repository.
    r = await client.get(
        "/rest/v1/transformations/jobs/summary",
        headers={"authorization": f"Bearer {scoped_token}"},
    )
    assert r.status_code == 200
    assert r.json()["total"] == 0
    assert set(r.json()["counts"].values()) == {0}


@pytest_asyncio.fixture
async def scoped_token(db):
    """A credential confined to a repository the fixtures never create."""
    from datum_sync import tokens

    await db.execute("DELETE FROM service_accounts WHERE name = '_pytest_scoped_api'")
    account_id = await db.fetchval(
        """
        INSERT INTO service_accounts (name, max_tier, repo_scope)
        VALUES ('_pytest_scoped_api', 4, ARRAY['Elsewhere/*'])
        RETURNING id
        """
    )
    _, raw = await tokens.create(db, account_id, "fixture")
    yield raw
    await db.execute("DELETE FROM service_accounts WHERE name = '_pytest_scoped_api'")
