-- Issue #147, AC-BI-003/AC-BI-004: copy every access_role_grant_events row
-- into audit_events with the exact field mapping below, then drop the old
-- table. Both statements run inside migration_runner.py's own single
-- per-file transaction (PLAN.md §4) -- a failure partway rolls back both,
-- per AC-BI-004 -- no explicit BEGIN/COMMIT/ROLLBACK is added here.
--
-- Field mapping (AC-BI-003):
--   action        = 'access_role.' || event_type   (event_type is one of
--                    'bootstrap' | 'grant' | 'revoke' | 'bootstrap_rejected',
--                    the exact CHECK-constrained set 0002+0003 define)
--   resource_type = 'principal'
--   resource_id   = target_subject                 (target_issuer is not
--                    separately preserved -- audit_events has no
--                    resource_issuer column, see 0004_audit_events.sql)
--   actor_subject/actor_issuer, occurred_at         preserved verbatim
--   details       = {"access_role": <original access_role>}
--   outcome       = 'rejected' for event_type = 'bootstrap_rejected',
--                    'applied' for every other event_type -- AC-BI-003's own
--                    text does not specify outcome explicitly -- this mapping
--                    is a considered design decision (bootstrap_rejected IS
--                    a rejection by construction), not a literal AC-BI-003
--                    quote.
--   id            preserved verbatim (same uuid), so AC-BI-003's "exactly
--                  one row per original row" is directly provable by an
--                  id-based join/count between the two tables pre-drop.
--
-- Rows migrated by this INSERT...SELECT never pass through
-- AuditStore.record's Pydantic validation (they are written by raw SQL, not
-- through the application-layer registry) -- so a migrated
-- access_role.bootstrap_rejected row's details will be
-- {"access_role": "SystemOwner"} (the *old* schema's shape, since
-- store.py's own pre-#147 _INSERT_GRANT_EVENT call always set access_role
-- even for bootstrap_rejected), not {"reason_code":
-- "bootstrap_identity_mismatch"} (the *new*, AccessRoleBootstrapRejectedDetails
-- shape every *future* access_role.bootstrap_rejected row written via
-- AuditStore.record will have going forward). This is expected and correct
-- -- pre-existing data predates the typed-model registry and is never
-- retroactively reshaped.

INSERT INTO audit_events (
    id, occurred_at, actor_subject, actor_issuer, action, resource_type,
    resource_id, outcome, details
)
SELECT
    id,
    occurred_at,
    actor_subject,
    actor_issuer,
    'access_role.' || event_type,
    'principal',
    target_subject,
    CASE WHEN event_type = 'bootstrap_rejected' THEN 'rejected' ELSE 'applied' END,
    jsonb_build_object('access_role', access_role)
FROM access_role_grant_events;

DROP TABLE access_role_grant_events;
