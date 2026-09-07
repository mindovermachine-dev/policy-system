"""Domain-specific exception types for the internal-seed Ingestion Adapter.

Adapter-local, per this repo's existing cross-layer translation pattern
(`_run_stage`/`_classify_stage_failure` in `ps_service/api/ingestion_orchestration.py`
translate a component-local error into an API-layer `ApiError` subclass at the
orchestration boundary) -- this module never imports `ps_service.api`.
"""

from __future__ import annotations


class InternalSeedError(Exception):
    """A submitted internal-regulation seed document is invalid.

    Raised by `schema.validate_seed_document` (JSON Schema structural layer,
    D7) naming the specific violation's document path and message -- never a
    generic "invalid JSON" message (AC-BI-019). `persist.py`'s own
    referential-integrity/cardinality checks (dangling edges, exactly-one-
    `HAS`-per-Obligation) raise this same type in a later slice (S2).
    """
