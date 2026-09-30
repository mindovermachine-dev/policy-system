-- Audit component baseline (issues #130, #147): the one generic, insert-only
-- audit trail table for every PS Service component that records who did what
-- to what. No application code ever issues UPDATE/DELETE against it --
-- enforced by omission (AuditStore exposes no such method at all).
--
-- Rule: migrations are append-only from the first deployment onward -- never
-- edit, renumber or delete an applied file, add a new file instead (see
-- ps_service.persistence.migration_runner).
--
-- `action` deliberately carries NO CHECK constraint: validity is enforced at
-- the application layer by AuditStore.record()'s typed-model registry, so a
-- new action never requires a schema migration to become insertable.
-- gen_random_uuid() is a Postgres 13+ built-in (no pgcrypto extension).

CREATE TABLE audit_events (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    occurred_at timestamptz NOT NULL DEFAULT now(),
    actor_subject text NOT NULL,
    actor_issuer text NOT NULL,
    action text NOT NULL,
    resource_type text NOT NULL,
    resource_id text NOT NULL,
    outcome text NOT NULL CHECK (outcome IN ('applied', 'rejected', 'failed')),
    details jsonb NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX audit_events_resource_idx ON audit_events (resource_type, resource_id);
CREATE INDEX audit_events_actor_idx ON audit_events (actor_subject, actor_issuer);
CREATE INDEX audit_events_occurred_at_idx ON audit_events (occurred_at);
