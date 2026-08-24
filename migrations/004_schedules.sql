-- 004_schedules.sql
-- Schedules and automations (build step 7).
--
-- The scheduler keeps no state of its own. `next_run` in this table is the
-- single source of truth for when a schedule is due: the worker claims due
-- rows the same way it claims jobs, submits, and writes the following
-- next_run back. An in-process scheduler would have to be told about every
-- edit made through the API -- which runs in a different process -- and would
-- hold a next_run that could disagree with the one on screen. This way an
-- edit is an UPDATE and nothing needs to hear about it.


-- Schedules ----------------------------------------------------------------
-- Exactly one of cron / interval_s is set. The CHECK is what makes "compute
-- the next run" total: no row can arrive at that code with both or neither.

CREATE TABLE schedules (
    id          SERIAL PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    repository  TEXT NOT NULL,
    workspace   TEXT NOT NULL,
    params      JSONB NOT NULL DEFAULT '{}'::jsonb,
    cron        TEXT,
    interval_s  INTEGER CHECK (interval_s IS NULL OR interval_s > 0),
    timezone    TEXT NOT NULL DEFAULT 'Pacific/Auckland',
    enabled     BOOLEAN NOT NULL DEFAULT true,
    created_by  TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_run    TIMESTAMPTZ,
    last_job    UUID REFERENCES jobs(id) ON DELETE SET NULL,
    next_run    TIMESTAMPTZ,

    CONSTRAINT schedules_one_trigger CHECK ((cron IS NULL) <> (interval_s IS NULL))
);

-- The due-claim reads only enabled rows, and only ever asks for the earliest.
CREATE INDEX schedules_due_idx ON schedules (next_run) WHERE enabled;


-- Automations --------------------------------------------------------------
-- The YAML the author wrote is stored verbatim alongside the parsed form, for
-- the same reason the workspaces table stores the manifest verbatim: what is
-- shown in the editor must be what was submitted, comments and all, and a
-- round trip through a YAML dumper is not that.
--
-- `config` is the validated parse. It exists so the trigger match is an
-- indexed JSONB read rather than a YAML parse per finished job.

CREATE TABLE automations (
    id           SERIAL PRIMARY KEY,
    name         TEXT NOT NULL UNIQUE,
    yaml         TEXT NOT NULL,
    config       JSONB NOT NULL,
    enabled      BOOLEAN NOT NULL DEFAULT true,
    created_by   TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_fired   TIMESTAMPTZ,
    last_error   TEXT
);

CREATE INDEX automations_trigger_idx
    ON automations ((config -> 'trigger' ->> 'type')) WHERE enabled;


-- Automation runs ----------------------------------------------------------
-- One row per firing, so "did it run, and what happened" is answerable
-- without reading a log file. An action that fails is recorded here and does
-- not stop the ones after it -- an automation is a list of independent
-- deliveries, not a transaction.

CREATE TABLE automation_runs (
    id            BIGSERIAL PRIMARY KEY,
    automation_id INTEGER NOT NULL REFERENCES automations(id) ON DELETE CASCADE,
    fired_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    trigger_job   UUID REFERENCES jobs(id) ON DELETE SET NULL,
    results       JSONB NOT NULL DEFAULT '[]'::jsonb,
    ok            BOOLEAN NOT NULL
);

CREATE INDEX automation_runs_recent_idx
    ON automation_runs (automation_id, fired_at DESC);


-- Where a job came from ----------------------------------------------------
-- A scheduled or automated job is otherwise indistinguishable from one a
-- person submitted, which makes "why did this run?" unanswerable. parent_job
-- already links a resubmit to its original; this says what caused the first
-- one. NULL means a person or an API client asked directly.

ALTER TABLE jobs ADD COLUMN triggered_by TEXT
    CHECK (triggered_by IS NULL OR triggered_by ~ '^(schedule|automation):');


-- Whether automations have considered this job ------------------------------
-- A job finishing announces itself on job_events, but a NOTIFY nobody is
-- listening for is gone -- and a worker restarting across a job's completion
-- is exactly when automations would silently not fire. So the notification is
-- only a hint to look early, and this column is the record: NULL means no
-- automation has yet been given the chance, and the worker claims those rows
-- on its poll the same way it claims queued jobs.

ALTER TABLE jobs ADD COLUMN automations_at TIMESTAMPTZ;

-- Every job that finished before this migration ran finished before automations
-- existed, so none of them is awaiting anything. Without this backfill the
-- column's NULL is read as "not yet considered" and the first worker poll after
-- deploying walks the entire job history. That is not theoretical: it is what
-- this migration did on its first run here, and an automation fired on a job
-- from a previous session.
UPDATE jobs SET automations_at = completed_at WHERE completed_at IS NOT NULL;

CREATE INDEX jobs_pending_automations_idx ON jobs (completed_at)
    WHERE automations_at IS NULL AND completed_at IS NOT NULL;
