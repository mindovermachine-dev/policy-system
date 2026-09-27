-- PLAN.md §1.2 (issue #133): permanent, insert-only audit trail (AC-BI-015).
-- gen_random_uuid() is a Postgres 13+ built-in (no pgcrypto extension
-- required), matching passkey_signing/migrations/0001_pending_approvals.sql's
-- own precedent. No application code ever issues UPDATE/DELETE against this
-- table -- "retained permanently" is enforced by omission (ps_service.authz
-- never does it), the same way L1's Immutability principle is applied
-- everywhere else in this codebase.

CREATE TABLE access_role_grant_events (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    event_type text NOT NULL CHECK (event_type IN ('bootstrap', 'grant', 'revoke')),
    actor_subject text NOT NULL,
    actor_issuer text NOT NULL,
    target_subject text NOT NULL,
    target_issuer text NOT NULL,
    access_role text NOT NULL,
    occurred_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX access_role_grant_events_target_idx ON access_role_grant_events (target_subject, target_issuer);
