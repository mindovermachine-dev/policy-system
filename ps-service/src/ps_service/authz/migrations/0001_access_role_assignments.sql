-- PLAN.md §1.1 (issue #133): current-state materialized view of every
-- principal's active AccessRole grants. A revoke is a DELETE of the one
-- matching row (never a soft-delete column) -- the permanent record of the
-- revoke event lives in access_role_grant_events (0002), not here.

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
