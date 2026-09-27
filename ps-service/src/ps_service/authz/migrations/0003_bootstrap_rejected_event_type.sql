-- PLAN.md §1 D-5 (issue #144): widen access_role_grant_events.event_type's
-- CHECK constraint to admit 'bootstrap_rejected' -- the audit event recorded
-- when bootstrap_first_owner rejects a principal that does not match the
-- operator-configured expected first-owner identity (AC-BI-005).
--
-- Constraint name verified live against a local postgres:16-alpine instance
-- (matching psServiceSigning.postgres.image in charts/policy-system/values.yaml)
-- via `\d access_role_grant_events` / pg_constraint after applying 0001+0002:
-- the unnamed table-level CHECK on event_type auto-named to
-- access_role_grant_events_event_type_check, exactly as CHANGES.md item 3
-- predicted from Postgres's standard {table}_{column}_check convention.

ALTER TABLE access_role_grant_events DROP CONSTRAINT access_role_grant_events_event_type_check;

ALTER TABLE access_role_grant_events ADD CONSTRAINT access_role_grant_events_event_type_check
    CHECK (event_type IN ('bootstrap', 'grant', 'revoke', 'bootstrap_rejected'));
