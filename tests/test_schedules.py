"""Schedules: due-time arithmetic, the claim, and the routes.

The arithmetic tests do not touch the database. They are the part most likely
to be wrong and the part hardest to notice being wrong -- a schedule that fires
an hour late twice a year looks like a schedule that fires, and nobody reads
the timestamps until something downstream breaks.
"""
from __future__ import annotations

import datetime as dt
import json

import asyncpg
import httpx
import pytest
import pytest_asyncio

from datum_sync import auth, jobs, schedules
from datum_sync import db as db_module
from datum_sync.api import app

UTC = dt.timezone.utc


# --------------------------------------------------------------------------
# due-time arithmetic
# --------------------------------------------------------------------------

def test_a_daily_cron_holds_its_local_hour_across_a_dst_change():
    """07:00 in Auckland is 19:00 UTC in winter and 18:00 UTC in summer.

    Evaluating the cron in UTC would keep the UTC hour fixed and move the local
    one, so "every weekday at 07:00" would silently become 08:00 for half the
    year. This is the reason `next_after` converts into the schedule's zone
    before asking croniter, and it is not visible in any test that runs inside
    one season.
    """
    # New Zealand DST begins on the last Sunday of September 2026 (the 27th).
    winter = schedules.next_after(
        dt.datetime(2026, 9, 20, 12, 0, tzinfo=UTC),
        "0 7 * * *", None, "Pacific/Auckland",
    )
    summer = schedules.next_after(
        dt.datetime(2026, 10, 20, 12, 0, tzinfo=UTC),
        "0 7 * * *", None, "Pacific/Auckland",
    )
    assert winter.hour == 19, winter
    assert summer.hour == 18, summer

    zone = schedules._zone("Pacific/Auckland")
    assert winter.astimezone(zone).hour == 7
    assert summer.astimezone(zone).hour == 7


def test_an_interval_is_a_duration_and_does_not_shift_with_the_clocks():
    """"Every 900 seconds" means 900 seconds, in any zone, in any season."""
    moment = dt.datetime(2026, 9, 26, 12, 0, tzinfo=UTC)
    for zone in ("UTC", "Pacific/Auckland", "America/New_York"):
        assert schedules.next_after(moment, None, 900, zone) - moment == \
            dt.timedelta(seconds=900)


@pytest.mark.parametrize("cron,interval,zone", [
    (None, None, "UTC"),                    # neither
    ("0 7 * * *", 900, "UTC"),              # both
    (None, 0, "UTC"),                       # non-positive interval
    (None, -1, "UTC"),                      # negative interval
    ("not a cron", None, "UTC"),            # unparseable
    ("0 7 * * *", None, "Mars/Olympus"),    # unknown zone
])
def test_a_definition_the_worker_could_not_run_is_refused(cron, interval, zone):
    """The CHECK constraint cannot tell whether a cron expression parses.

    Without this the row is accepted, sits enabled, and never fires -- the
    failure mode with no symptom.
    """
    with pytest.raises(schedules.ScheduleError):
        schedules.validate(cron, interval, zone)


def test_five_missed_days_are_one_run_and_not_five():
    """A nightly delivery does not deliver five times because the worker was down.

    The walk forward is what makes this true. Returning the missed time itself
    would fire immediately and then again on the next poll, once per day it
    owed, which for anything that emails or writes to a client folder is the
    kind of catch-up that has to be apologised for.
    """
    zone = schedules._zone("Pacific/Auckland")
    missed = dt.datetime(2026, 3, 1, 7, 0, tzinfo=zone).astimezone(UTC)
    row = {"next_run": missed, "cron": "0 7 * * *", "interval_s": None,
           "timezone": "Pacific/Auckland"}
    now = missed + dt.timedelta(days=5, hours=1, minutes=15)

    upcoming = schedules._advance(row, now)

    assert upcoming > now
    assert upcoming - now < dt.timedelta(days=1)
    assert upcoming.astimezone(zone).hour == 7


def test_a_missed_interval_keeps_its_phase():
    """An hourly schedule anchored on the hour stays on the hour.

    Deliberately an interval and not a cron. Advancing from `now` instead of
    from the missed time gives a cron schedule the identical answer -- its
    phase is written in the expression -- so a cron test of this passes against
    both versions and proves nothing, which is what the first one here did.
    Only an interval carries its phase in the timestamp.
    """
    anchored = dt.datetime(2026, 3, 1, 9, 0, tzinfo=UTC)
    row = {"next_run": anchored, "cron": None, "interval_s": 3600,
           "timezone": "UTC"}
    now = anchored + dt.timedelta(days=5, minutes=15)

    upcoming = schedules._advance(row, now)

    assert upcoming > now
    assert (upcoming.minute, upcoming.second) == (0, 0), upcoming


def test_a_pathologically_overdue_interval_gives_up_walking():
    """A 7-second interval a week behind is 86,400 steps of catch-up.

    The cap is not an optimisation. A worker spinning in that loop stops
    claiming jobs, so one badly-configured schedule takes the whole queue down.

    Asserted as an exact value rather than as elapsed time. A wall-clock bound
    is the obvious way to write this and it is a bad one twice over: it flakes
    on a loaded machine, and 600,000 timedelta additions take about half a
    second anyway -- so the first version of this test passed with the cap
    raised to a hundred million. Giving up re-times from `moment`, which lands
    off the schedule's own phase, and that is a fact no timer is needed to see.
    """
    moment = dt.datetime(2026, 3, 8, 12, 0, 30, tzinfo=UTC)
    behind = moment - dt.timedelta(days=7, seconds=3)
    row = {"next_run": behind, "cron": None, "interval_s": 7, "timezone": "UTC"}

    upcoming = schedules._advance(row, moment)

    # Re-timed from `moment`, not walked to `behind + k*7` (which would be
    # 3 seconds out of step with this).
    assert upcoming == moment + dt.timedelta(seconds=7), upcoming


# --------------------------------------------------------------------------
# the claim
# --------------------------------------------------------------------------

@pytest_asyncio.fixture
async def schedule(db, workspace):
    repo, ws = workspace
    row = await schedules.create(
        db, name="_pytest-schedule", repository=repo, workspace=ws,
        params={"WHO": "world"}, interval_s=900, created_by="_pytest",
    )
    yield row
    await db.execute("DELETE FROM schedules WHERE name = '_pytest-schedule'")


@pytest.mark.asyncio
async def test_a_schedule_that_is_not_due_does_not_fire(db, schedule):
    assert await schedules.run_due(db) == []


@pytest.mark.asyncio
async def test_a_due_schedule_submits_and_says_what_caused_the_job(db, schedule):
    await db.execute("UPDATE schedules SET next_run = now() - interval '1 minute' "
                     "WHERE id = $1", schedule["id"])

    fired = await schedules.run_due(db)

    assert len(fired) == 1 and fired[0]["error"] is None
    row = await db.fetchrow(
        "SELECT submitted_by, triggered_by, status FROM jobs WHERE id = $1",
        fired[0]["job_id"])
    assert row["triggered_by"] == "schedule:_pytest-schedule"
    assert row["status"] == "queued"


@pytest.mark.asyncio
async def test_firing_advances_next_run_so_it_does_not_submit_twice(db, schedule):
    await db.execute("UPDATE schedules SET next_run = now() - interval '1 minute' "
                     "WHERE id = $1", schedule["id"])

    assert len(await schedules.run_due(db)) == 1
    assert await schedules.run_due(db) == []
    assert await db.fetchval(
        "SELECT count(*) FROM jobs WHERE triggered_by = 'schedule:_pytest-schedule'"
    ) == 1


@pytest.mark.asyncio
async def test_a_schedule_pointing_at_nothing_records_it_and_still_advances(
    db, schedule
):
    """A workspace someone unpublished is an operational fact, not a crash.

    Advancing anyway is the point: without it the row stays due and the worker
    retries it on every poll, turning one broken schedule into a hot loop that
    also floods the log.
    """
    await db.execute(
        "UPDATE schedules SET workspace = 'gone', "
        "next_run = now() - interval '1 minute' WHERE id = $1", schedule["id"])

    fired = await schedules.run_due(db)

    assert len(fired) == 1
    assert fired[0]["job_id"] is None
    assert "WorkspaceNotFound" in fired[0]["error"]
    assert await db.fetchval(
        "SELECT next_run FROM schedules WHERE id = $1", schedule["id"]
    ) > dt.datetime.now(UTC)


@pytest.mark.asyncio
async def test_re_enabling_does_not_replay_the_runs_it_missed(db, schedule):
    """A schedule disabled for a week has a next_run a week in the past.

    Re-enabling it must not mean "fire immediately, then catch up" -- that turns
    a paused nightly job into a burst the moment someone switches it back on.
    """
    await db.execute(
        "UPDATE schedules SET enabled = false, "
        "next_run = now() - interval '7 days' WHERE id = $1", schedule["id"])

    row = await schedules.update(db, schedule["id"], {"enabled": True})

    assert row["next_run"] > dt.datetime.now(UTC)


@pytest.mark.asyncio
async def test_pausing_a_schedule_does_not_eat_its_parameters(db, schedule):
    """A patch that never mentions params must leave them a JSON object.

    Found in a browser, not here: pausing a schedule from the UI sends
    `{"enabled": false}` and nothing else, and the detail screen then failed on
    `'COUNT' in "{\\"COUNT\\": 7}"` -- the params had become a *string*. asyncpg
    hands back jsonb as JSON text, update() writes it with json.dumps, and a key
    carried through unchanged is therefore re-encoded once per patch.

    Patched twice on purpose. Once is enough to corrupt, but twice is what the
    bug actually looked like in the wild -- each pause wrapping the last -- and
    a test that only proves the first hop would pass against a fix that decodes
    but does not re-encode symmetrically.
    """
    for enabled in (False, True):
        row = await schedules.update(db, schedule["id"], {"enabled": enabled})
        assert json.loads(row["params"]) == {"WHO": "world"}, row["params"]


@pytest.mark.asyncio
async def test_a_parameter_the_workspace_does_not_accept_is_refused(db, workspace):
    """Validated against the published manifest at write time.

    Otherwise the schedule is accepted, and the failure is discovered at 3am by
    nobody -- as a job error naming a job nobody submitted.
    """
    repo, ws = workspace
    with pytest.raises(schedules.ScheduleError):
        await schedules.create(db, name="_pytest-bad", repository=repo,
                               workspace=ws, params={"NOPE": 1}, interval_s=900)


# --------------------------------------------------------------------------
# routes
# --------------------------------------------------------------------------

@pytest_asyncio.fixture
async def client(db, token):
    await db_module.init_pool()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver",
        headers={"authorization": f"Bearer {token}"},
    ) as c:
        yield c
    await db_module.close_pool()


@pytest_asyncio.fixture
async def scoped_token(db):
    """An account that can reach only a repository the fixtures do not use."""
    raw = auth.new_token()
    await db.execute("DELETE FROM service_accounts WHERE name = '_pytest_scoped'")
    await db.execute(
        """
        INSERT INTO service_accounts (name, token_hash, max_tier, repo_scope)
        VALUES ('_pytest_scoped', $1, 4, ARRAY['Elsewhere/*'])
        """,
        auth.hash_token(raw),
    )
    yield raw
    await db.execute("DELETE FROM service_accounts WHERE name = '_pytest_scoped'")


@pytest.mark.asyncio
async def test_creating_a_schedule_returns_when_it_will_next_run(client, workspace):
    repo, ws = workspace
    r = await client.post("/rest/v1/schedules", json={
        "name": "_pytest-route", "repository": repo, "workspace": ws,
        "params": {"WHO": "world"}, "interval_s": 900,
    })
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["next_run"] is not None
    assert body["created_by"] == "_pytest"
    await client.delete(f"/rest/v1/schedules/{body['id']}")


@pytest.mark.asyncio
async def test_a_bad_cron_is_a_400_on_the_request_that_wrote_it(client, workspace):
    repo, ws = workspace
    r = await client.post("/rest/v1/schedules", json={
        "name": "_pytest-route", "repository": repo, "workspace": ws,
        "params": {"WHO": "world"}, "cron": "every tuesday please",
    })
    assert r.status_code == 400
    assert r.json()["code"] == "INVALID_PARAMETER"


@pytest.mark.asyncio
async def test_a_scoped_caller_cannot_schedule_another_repository(
    client, db, workspace, scoped_token
):
    """The schedule submits jobs later, with nobody present to check scope.

    `jobs.submit` performs no scope check -- that has always been the API's
    job -- so a missing check here is a way to have the server run a workspace
    the caller could not run themselves.
    """
    repo, ws = workspace
    # Establish the precondition rather than assume it. `break_the_guard.py`
    # runs this test with the scope check deleted, at which point the row IS
    # created and nothing here removes it -- so the next honest run fails on
    # the previous dishonest one's leftovers.
    await db.execute("DELETE FROM schedules WHERE name = '_pytest-esc'")

    r = await client.post(
        "/rest/v1/schedules",
        json={"name": "_pytest-esc", "repository": repo, "workspace": ws,
              "params": {"WHO": "world"}, "interval_s": 900},
        headers={"authorization": f"Bearer {scoped_token}"},
    )
    assert r.status_code == 403, r.text
    assert await db.fetchval(
        "SELECT count(*) FROM schedules WHERE name = '_pytest-esc'") == 0


@pytest.mark.asyncio
async def test_a_scoped_caller_does_not_see_other_repositories_schedules(
    client, schedule, scoped_token
):
    mine = await client.get("/rest/v1/schedules")
    assert any(s["name"] == "_pytest-schedule" for s in mine.json()["items"])

    theirs = await client.get(
        "/rest/v1/schedules",
        headers={"authorization": f"Bearer {scoped_token}"})
    assert theirs.status_code == 200
    assert not any(s["name"] == "_pytest-schedule"
                   for s in theirs.json()["items"])


@pytest.mark.asyncio
async def test_a_schedule_cannot_be_repointed_at_another_workspace(client, schedule):
    """Repository and workspace are not patchable.

    A schedule that could be repointed is a scope check that happened once, on
    a row that no longer says what it said when it happened.
    """
    r = await client.patch(f"/rest/v1/schedules/{schedule['id']}",
                           json={"repository": "Elsewhere"})
    assert r.status_code == 400
    assert "cannot change" in r.json()["message"]
