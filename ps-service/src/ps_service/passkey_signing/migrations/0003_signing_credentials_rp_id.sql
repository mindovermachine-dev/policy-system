-- Issue #196: record the WebAuthn rp_id each credential was enrolled under.
-- A credential only works for the RP id that created it, so needs_enrollment
-- and allowCredentials must be scoped to the host the browser is actually on;
-- without this column they were scoped to the actor alone, so enrolling on one
-- host reported "already enrolled" on every other host and offered a
-- credential the authenticator could never match.
--
-- Deliberately nullable with no backfill: a row written before this column
-- existed was enrolled under an rp_id nobody recorded, so it cannot be proven
-- usable on any particular host. The store's equality predicate excludes NULL
-- on its own, which is exactly the wanted behaviour -- such a row is never
-- offered and never suppresses a fresh enrollment (AC-BI-015).

ALTER TABLE signing_credentials ADD COLUMN rp_id text;

CREATE INDEX signing_credentials_actor_rp_idx
    ON signing_credentials (actor_subject, actor_issuer, rp_id);
