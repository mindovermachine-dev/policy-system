-- Ingestion Runs component baseline (issue #194): one row per start_ingestion
-- submission, recording the run's status and, once finished, its result or
-- its sanitized error text. The permanent who-did-what record lives in
-- audit_events (audit component), not here.
--
-- Rule: migrations are append-only from the first deployment onward -- never
-- edit, renumber or delete an applied file, add a new file instead (see
-- ps_service.persistence.migration_runner).
--
-- run_id is supplied by the application (the uuid4 bind_run_context binds for
-- the submitting start_ingestion call), never defaulted here, so one id
-- correlates log entries, the live-stage registry, audit_events and this row.
-- Every query this component issues is a primary-key lookup or update, so no
-- secondary index is created.

CREATE TABLE ingestion_runs (
    run_id uuid PRIMARY KEY,
    celex text NOT NULL,
    short_name text NOT NULL,
    actor_subject text NOT NULL,
    actor_issuer text NOT NULL,
    status text NOT NULL DEFAULT 'running'
        CHECK (status IN ('running', 'succeeded', 'failed')),
    result jsonb,
    error text,
    submitted_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    CHECK (status <> 'succeeded' OR result IS NOT NULL),
    CHECK (status <> 'failed' OR error IS NOT NULL),
    CHECK ((status = 'running') = (finished_at IS NULL))
);
