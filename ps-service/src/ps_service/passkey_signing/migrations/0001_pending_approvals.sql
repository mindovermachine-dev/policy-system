-- PLAN.md §1.1 (issue #131): pending signed-passkey-approval records for
-- near_misses_resolve(decision="merge"). gen_random_uuid() is a Postgres
-- 13+ built-in (no pgcrypto extension required).

CREATE TABLE pending_approvals (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    code_hash bytea NOT NULL,
    tool_name text NOT NULL,
    normalized_args jsonb NOT NULL,
    actor_subject text NOT NULL,
    actor_issuer text NOT NULL,
    nonce bytea NOT NULL,
    display_summary jsonb NOT NULL,
    status text NOT NULL DEFAULT 'pending',
    outcome jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL DEFAULT (now() + interval '15 minutes')
);

CREATE UNIQUE INDEX pending_approvals_code_hash_idx ON pending_approvals (code_hash);
CREATE INDEX pending_approvals_actor_idx ON pending_approvals (actor_subject, actor_issuer);
