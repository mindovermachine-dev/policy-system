-- Issue #147: one generic, insert-only audit trail table replacing
-- access_role_grant_events (issue #133) and every future per-feature audit
-- table (#134/#136/#140). gen_random_uuid() mirrors
-- 0002_access_role_grant_events.sql's own precedent (Postgres 13+ built-in,
-- no pgcrypto extension). No application code ever issues UPDATE/DELETE
-- against this table -- enforced by omission (AuditStore exposes no such
-- method at all), the same convention 0002's own header comment already
-- states for the table this migration will eventually replace (the copy +
-- drop migration is 0005, a later slice -- this migration only creates the
-- new table, and access_role_grant_events is left untouched here).
--
-- `action` deliberately carries NO CHECK constraint (unlike
-- access_role_grant_events.event_type's CHECK, which needed a follow-on
-- migration -- 0003 -- just to widen it for one new value): validity is
-- enforced at the application layer by AuditStore.record()'s typed-model
-- registry instead, precisely so a new action (#134/#136/#140) never
-- requires a schema migration to become insertable.

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
