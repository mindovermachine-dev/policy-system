-- Runtime-config component baseline (issue #130): current value of every runtime-mutable
-- configuration key. One row per key that has been set, and a reset deletes the row (the key
-- then falls back to its env-var/default). The permanent record of every set and reset
-- lives in audit_events (audit component), not here.
--
-- Which keys exist, and which values are valid for each, is enforced by the application-layer
-- key registry (ps_service.runtime_config.registry), not by a CHECK constraint, so a new key
-- never requires a schema migration.
--
-- Rule: migrations are append-only from the first deployment onward -- never
-- edit, renumber or delete an applied file, add a new file instead (see
-- ps_service.persistence.migration_runner).

CREATE TABLE runtime_config (
    key text PRIMARY KEY,
    value jsonb NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);
