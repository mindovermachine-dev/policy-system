-- Audit component (issue #195): expression indexes backing the allow-listed
-- `details` exact-match filter of `list-audit-events` ("who ingested or
-- restored instrument X?"). One index per key in
-- `ps_service.audit.models.AUDIT_DETAILS_FILTER_KEYS` -- the indexed expression
-- text must match the fixed SQL fragment in `ps_service.audit.store` exactly.
--
-- Rule: migrations are append-only from the first deployment onward -- never
-- edit, renumber or delete an applied file, add a new file instead (see
-- ps_service.persistence.migration_runner).
--
-- Plain CREATE INDEX, not CONCURRENTLY: the runner wraps each file in one
-- transaction, in which CONCURRENTLY is not allowed. No table is created or
-- altered, so this is compatible with a later partitioning of audit_events.

CREATE INDEX audit_events_details_celex_idx ON audit_events ((details ->> 'celex'));
CREATE INDEX audit_events_details_regulatory_instrument_id_idx ON audit_events ((details ->> 'regulatory_instrument_id'));
CREATE INDEX audit_events_details_instrument_id_idx ON audit_events ((details ->> 'instrument_id'));
