-- OAuth consent sessions may be 15 minutes for a demo or 2, 4, or 8 hours.
ALTER TABLE plane_authorizations
    DROP CONSTRAINT plane_authorizations_duration_seconds_check;
ALTER TABLE plane_authorizations
    ADD CONSTRAINT plane_authorizations_duration_seconds_check
    CHECK (duration_seconds BETWEEN 60 AND 28800);
