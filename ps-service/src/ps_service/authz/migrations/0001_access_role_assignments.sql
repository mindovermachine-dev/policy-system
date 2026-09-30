-- Access-roles component baseline (issues #130, #133): current-state
-- materialized view of every principal's active AccessRole grants. A revoke
-- is a DELETE of the one matching row (never a soft-delete column) -- the
-- permanent record of grants and revokes lives in audit_events (audit
-- component), not here.
--
-- Rule: migrations are append-only from the first deployment onward -- never
-- edit, renumber or delete an applied file, add a new file instead (see
-- ps_service.persistence.migration_runner).

CREATE TABLE access_role_assignments (
    principal_subject text NOT NULL,
    principal_issuer text NOT NULL,
    access_role text NOT NULL,
    granted_at timestamptz NOT NULL DEFAULT now(),
    granted_by_subject text NOT NULL,
    granted_by_issuer text NOT NULL,
    PRIMARY KEY (principal_subject, principal_issuer, access_role)
);

CREATE INDEX access_role_assignments_role_idx ON access_role_assignments (access_role);
