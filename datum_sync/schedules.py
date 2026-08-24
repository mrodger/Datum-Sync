"""Schedules: run a workspace on a cron expression or a fixed interval.

There is no scheduler object. `schedules.next_run` is the only place a due
time is recorded, and the worker claims due rows exactly the way it claims
jobs -- `FOR UPDATE SKIP LOCKED`, inside a transaction. That choice is the
whole design:

  * A schedule created or edited through the API takes effect immediately,
    because the API writes the same column the worker reads. Nothing has to be
    told about the edit and nothing can be out of date.
  * A restart loses nothing. An in-process scheduler rebuilds its timers from
    the table on startup and is blind to anything that changed while it was
    down; here the table never stopped being right.
  * "When does this next run?" has one answer, and it is the answer the UI
    shows.

The cost is that a schedule fires within one worker poll (5s) of its due time
rather than on the second. For a workspace runner that is not a real cost.
"""
from __future__ import annotations

import datetime as dt
import json
import zoneinfo
from typing import Any

import asyncpg
from croniter import croniter

from datum_sync import jobs
from datum_sync.manifest import ParameterError


class ScheduleError(Exception):
    """A schedule definition that cannot be stored or cannot be advanced."""


def _zone(name: str) -> zoneinfo.ZoneInfo:
    try:
        return zoneinfo.ZoneInfo(name)
    except zoneinfo.ZoneInfoNotFoundError:
        raise ScheduleError(f"unknown timezone: {name}") from None


def validate(
    cron: str | None, interval_s: int | None, timezone: str
) -> None:
    """Reject a definition the database would accept but the worker could not run.

    The CHECK constraint enforces that exactly one of cron/interval_s is set;
    it cannot tell whether the cron expression parses. Doing that here means a
    bad expression is a 400 on the request that wrote it, rather than a
    schedule that sits enabled and silently never fires.
    """
    if (cron is None) == (interval_s is None):
        raise ScheduleError("set exactly one of cron or interval_s")
    if interval_s is not None and interval_s <= 0:
        raise ScheduleError("interval_s must be positive")
    _zone(timezone)
    if cron is not None and not croniter.is_valid(cron):
        raise ScheduleError(f"not a cron expression: {cron!r}")


def next_after(
    moment: dt.datetime,
    cron: str | None,
    interval_s: int | None,
    timezone: str,
) -> dt.datetime:
    """The first run strictly after `moment`, as UTC.

    Cron is evaluated in the schedule's own timezone and converted back, which
    is the only way "every weekday at 07:00" survives a daylight saving change
    -- 07:00 NZST and 07:00 NZDT are different instants, and the schedule means
    the local one.

    Intervals are deliberately not timezone-aware: "every 900 seconds" is a
    duration, and a duration does not shift when the clocks do.
    """
    if interval_s is not None:
        return moment + dt.timedelta(seconds=interval_s)

    zone = _zone(timezone)
    local = moment.astimezone(zone)
    following = croniter(cron, local).get_next(dt.datetime)
    return following.astimezone(dt.timezone.utc)


def _check_params(manifest, params: dict[str, Any]) -> None:
    """Validate against the published manifest, as ScheduleError.

    jobs.submit does this and raises JobError; a schedule is not a job, and the
    caller here is writing a schedule, so it gets the error its own operation
    can raise. Without the translation a bad parameter on a schedule would
    surface as a job error naming a job that was never created.
    """
    try:
        jobs.validate_params(manifest, params)
    except ParameterError as e:
        raise ScheduleError(str(e)) from None


# -- CRUD --------------------------------------------------------------------

_COLUMNS = """
    id, name, repository, workspace, params, cron, interval_s, timezone,
    enabled, created_by, created_at, last_run, last_job, next_run
"""


async def create(
    conn: asyncpg.Connection,
    name: str,
    repository: str,
    workspace: str,
    params: dict[str, Any] | None = None,
    cron: str | None = None,
    interval_s: int | None = None,
    timezone: str = "Pacific/Auckland",
    enabled: bool = True,
    created_by: str | None = None,
) -> asyncpg.Record:
    validate(cron, interval_s, timezone)

    # Validated against the published manifest now, so an unrunnable schedule
    # is refused at creation rather than discovered at 3am by nobody. This is
    # the same call jobs.submit makes, and it raises the same errors.
    manifest = await jobs.load_manifest_for(conn, repository, workspace)
    _check_params(manifest, params or {})

    row = await conn.fetchrow(
        f"""
        INSERT INTO schedules (name, repository, workspace, params, cron,
                               interval_s, timezone, enabled, created_by, next_run)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
        RETURNING {_COLUMNS}
        """,
        name, repository, workspace, json.dumps(params or {}), cron,
        interval_s, timezone, enabled, created_by,
        next_after(dt.datetime.now(dt.timezone.utc), cron, interval_s, timezone),
    )
    return row


async def get(conn: asyncpg.Connection, schedule_id: int) -> asyncpg.Record | None:
    return await conn.fetchrow(
        f"SELECT {_COLUMNS} FROM schedules WHERE id = $1", schedule_id
    )


async def listing(conn: asyncpg.Connection) -> list[asyncpg.Record]:
    return await conn.fetch(
        f"SELECT {_COLUMNS} FROM schedules ORDER BY enabled DESC, next_run, name"
    )


_PATCHABLE = ("params", "cron", "interval_s", "timezone", "enabled")


async def update(
    conn: asyncpg.Connection, schedule_id: int, changes: dict[str, Any]
) -> asyncpg.Record | None:
    """Apply a partial update. Recomputes next_run when the trigger changes.

    Enabling a schedule also recomputes it. A schedule disabled for a week has
    a next_run a week in the past, and re-enabling it must not mean "fire
    immediately, then catch up" -- the missed runs are missed, and pretending
    otherwise turns a paused nightly job into a burst.
    """
    unknown = set(changes) - set(_PATCHABLE)
    if unknown:
        raise ScheduleError(f"cannot change: {', '.join(sorted(unknown))}")

    current = await get(conn, schedule_id)
    if current is None:
        return None

    merged = {key: current[key] for key in _PATCHABLE}
    # Decoded before merging, because the row carries params as JSON *text* --
    # that is what asyncpg returns for jsonb -- and this function writes them
    # back with json.dumps. Carrying the text through unchanged re-encodes it,
    # so a patch that only flips `enabled` stores a JSON string where an object
    # was. Every later read then yields a string, the parameters silently stop
    # being parameters, and the next patch wraps them again.
    merged["params"] = json.loads(current["params"])
    merged.update(changes)
    validate(merged["cron"], merged["interval_s"], merged["timezone"])

    if "params" in changes:
        manifest = await jobs.load_manifest_for(
            conn, current["repository"], current["workspace"]
        )
        _check_params(manifest, merged["params"])

    retimed = any(key in changes for key in ("cron", "interval_s", "timezone"))
    enabling = changes.get("enabled") and not current["enabled"]
    next_run = current["next_run"]
    if retimed or enabling:
        next_run = next_after(
            dt.datetime.now(dt.timezone.utc),
            merged["cron"], merged["interval_s"], merged["timezone"],
        )

    return await conn.fetchrow(
        f"""
        UPDATE schedules
        SET params = $2, cron = $3, interval_s = $4, timezone = $5,
            enabled = $6, next_run = $7
        WHERE id = $1
        RETURNING {_COLUMNS}
        """,
        schedule_id, json.dumps(merged["params"]), merged["cron"],
        merged["interval_s"], merged["timezone"], merged["enabled"], next_run,
    )


async def delete(conn: asyncpg.Connection, schedule_id: int) -> bool:
    result = await conn.execute("DELETE FROM schedules WHERE id = $1", schedule_id)
    return result.endswith(" 1")


# -- the tick ----------------------------------------------------------------

# A schedule whose due time is far in the past has to be walked forward one
# period at a time, and a 1-second interval left behind for a week is 600,000
# steps. Past this many, stop walking and re-time from now: the schedule has
# missed so much that its original phase is not worth preserving, and a worker
# blocked in a loop is worse than a schedule that resumes on a new offset.
_MAX_CATCHUP_STEPS = 1000


def _advance(row: asyncpg.Record, moment: dt.datetime) -> dt.datetime:
    """The next due time after `moment`, walking forward from the missed one.

    Walking on until the result is in the future is what stops a schedule
    firing once for every period it missed -- catching up on a nightly job by
    running it seven times in a row is not what anyone meant by a nightly job.

    Starting from the *due* time rather than from `moment` is what preserves an
    interval's phase: an hourly schedule anchored on the hour stays on the
    hour, instead of moving to :15 because that is when the worker came back
    and staying there. It makes no difference to a cron schedule, whose phase
    is written in the expression -- which is worth saying because the obvious
    justification ("otherwise 07:00 becomes 08:15") is not true, and a test
    written to check it passes either way.
    """
    upcoming = row["next_run"]
    for _ in range(_MAX_CATCHUP_STEPS):
        upcoming = next_after(upcoming, row["cron"], row["interval_s"], row["timezone"])
        if upcoming > moment:
            return upcoming
    return next_after(moment, row["cron"], row["interval_s"], row["timezone"])


async def run_due(conn: asyncpg.Connection, now: dt.datetime | None = None) -> list[dict]:
    """Submit a job for every schedule that is due. Returns what fired.

    One transaction per schedule, not one for all of them: a schedule whose
    workspace has since been unpublished must not prevent the others from
    running. Its own row is still advanced, so a broken schedule reports the
    same failure once per period instead of retrying in a tight loop.
    """
    due = await conn.fetch(
        """
        SELECT id FROM schedules
        WHERE enabled AND next_run IS NOT NULL AND next_run <= $1
        ORDER BY next_run
        """,
        now or dt.datetime.now(dt.timezone.utc),
    )

    fired = []
    for row in due:
        fired.append(await _fire(conn, row["id"], now))
    return [f for f in fired if f is not None]


async def _fire(
    conn: asyncpg.Connection, schedule_id: int, now: dt.datetime | None
) -> dict | None:
    moment = now or dt.datetime.now(dt.timezone.utc)
    async with conn.transaction():
        # SKIP LOCKED rather than a plain SELECT: the advisory lock already
        # means one worker, but this also makes the claim correct if that ever
        # stops being true, and it is the same shape as jobs.claim().
        row = await conn.fetchrow(
            """
            SELECT id, name, repository, workspace, params, cron, interval_s,
                   timezone, next_run
            FROM schedules
            WHERE id = $1 AND enabled AND next_run <= $2
            FOR UPDATE SKIP LOCKED
            """,
            schedule_id, moment,
        )
        if row is None:
            return None

        upcoming = _advance(row, moment)

        job_id = None
        error = None
        try:
            job_id = await jobs.submit(
                conn,
                row["repository"],
                row["workspace"],
                json.loads(row["params"]),
                submitted_by=f"schedule:{row['name']}",
                triggered_by=f"schedule:{row['name']}",
            )
        except Exception as exc:  # noqa: BLE001 - recorded, never raised onward
            # A schedule pointing at an unpublished workspace is a normal
            # operational state, not a worker crash.
            error = f"{type(exc).__name__}: {exc}"

        await conn.execute(
            "UPDATE schedules SET last_run = $2, last_job = $3, next_run = $4 "
            "WHERE id = $1",
            schedule_id, moment, job_id, upcoming,
        )

    return {"schedule": row["name"], "job_id": job_id, "error": error,
            "next_run": upcoming}
