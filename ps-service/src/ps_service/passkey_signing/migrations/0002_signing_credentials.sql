-- PLAN.md §1.2 (issue #131, Slice 2): enrolled WebAuthn signing credentials
-- for PS Service's own transaction-signing relying party (independent of
-- Authentik's login-time WebAuthn, PLAN.md §0.3). One row per authenticator
-- an actor has enrolled. gen_random_uuid() is a Postgres 13+ built-in (no
-- pgcrypto extension required).

CREATE TABLE signing_credentials (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    actor_subject text NOT NULL,
    actor_issuer text NOT NULL,
    credential_id bytea NOT NULL UNIQUE,
    public_key bytea NOT NULL,
    sign_count bigint NOT NULL DEFAULT 0,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX signing_credentials_actor_idx ON signing_credentials (actor_subject, actor_issuer);
