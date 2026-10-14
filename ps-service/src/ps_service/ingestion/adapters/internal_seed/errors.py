"""Domain-specific exception types for the internal-seed ingestion adapter.

The error type is local to the adapter. The API layer translates it into an `ApiError` subclass
at the orchestration boundary (`_run_stage`/`_classify_stage_failure` in
`ps_service/api/ingestion_orchestration.py`), so this module never imports `ps_service.api`.
"""

from __future__ import annotations


class InternalSeedError(Exception):
    """A submitted internal-regulation seed document is invalid, or its graphs cannot be written.

    Raised by `schema.validate_seed_document` with the violation's document path and message,
    never a generic "invalid JSON" message. Also raised by the `persist` and `reconcile` modules
    for referential-integrity and cardinality violations (dangling edges, not exactly one `HAS`
    per Obligation) and for graph-state problems that stop a submission before any write.

    The caller should treat the submission as rejected: fix the document or the graph state named
    in the message and resubmit.
    """
