-- Stable ownership and replayable job events for the second read-only job increment.
-- Historical jobs are linked only where an existing portal principal name matches
-- exactly; all other jobs retain the legacy submitted_by fallback.
ALTER TABLE jobs ADD COLUMN portal_principal_id INTEGER
    REFERENCES service_accounts(id) ON DELETE SET NULL;
UPDATE jobs j SET portal_principal_id=s.id
  FROM service_accounts s
 WHERE j.submitted_by=s.name AND s.portal_kind IN ('human','agent');
CREATE INDEX jobs_portal_principal_time
    ON jobs(portal_principal_id,submitted_at DESC) WHERE portal_principal_id IS NOT NULL;

CREATE TABLE job_events (
    id BIGSERIAL PRIMARY KEY,
    job_id UUID NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK(kind IN ('status','progress','log')),
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX job_events_job_cursor ON job_events(job_id,id);
