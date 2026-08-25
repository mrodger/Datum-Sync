-- Hosted services: what a job leaves running at /serve/{name}/.
--
-- A row here is not created by hand. It is created by a job completing with an
-- artifact whose *declared* output type is service/*, which means the manifest
-- named it and the publish gate saw it. That ordering is the point: a run
-- cannot invent a hosted service the workspace never published.

CREATE TABLE hosted_services (
    id          SERIAL PRIMARY KEY,

    -- The URL segment. Globally unique because the URL is /serve/{name}/ with
    -- nothing else in it -- two workspaces cannot both own "app". The publish
    -- gate refuses the second one rather than letting a job win the race at run
    -- time, which would move a live URL to a different application silently.
    name        TEXT NOT NULL UNIQUE,

    type        TEXT NOT NULL,

    -- Who owns it. Not in the original sketch, and needed for three things a
    -- name alone cannot answer: which services to drop when a workspace is
    -- deregistered, whether a caller scoped to one repository may see this row,
    -- and which workspace to re-run to refresh it.
    repository  TEXT NOT NULL,
    workspace   TEXT NOT NULL,

    -- The job whose artifacts are being served. ON DELETE SET NULL rather than
    -- CASCADE: deleting old job history must not silently take a live service
    -- off the air. The row survives with a dangling source and `path` still
    -- pointing at whatever is on disk, which is a visible, fixable state --
    -- unlike a URL that has quietly stopped existing.
    source_job  UUID REFERENCES jobs(id) ON DELETE SET NULL,

    -- Static types: the directory to serve. Absolute, and always inside a job's
    -- artifact directory -- see services.register.
    path        TEXT,

    -- Supervised types (service/interactive, service/notebook). Recorded so the
    -- row is complete and the UI can show what *would* run; nothing reads them
    -- yet. Supervision is not implemented: these register with status 'stopped'
    -- and /serve/ refuses them with a reason rather than 404, so an operator
    -- sees "not supported here" instead of "your job did nothing".
    command     TEXT,
    port        INTEGER,

    status      TEXT NOT NULL DEFAULT 'stopped'
                CHECK (status IN ('stopped', 'starting', 'running', 'error')),
    pid         INTEGER,
    started_at  TIMESTAMPTZ,
    last_health TIMESTAMPTZ,

    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- A static service with no path cannot be served and a supervised one with
    -- no command cannot be started. Both would be rows that exist only to fail
    -- a request later, so they cannot be written at all.
    CONSTRAINT hosted_services_runnable CHECK (
        (type LIKE 'service/%')
        AND (
            (type IN ('service/static', 'service/pwa', 'service/dashboard')
             AND path IS NOT NULL)
            OR
            (type IN ('service/interactive', 'service/notebook')
             AND command IS NOT NULL)
        )
    )
);

-- The Services screen lists by owner; deregistering a workspace deletes by it.
CREATE INDEX hosted_services_owner_idx ON hosted_services (repository, workspace);
