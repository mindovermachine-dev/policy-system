"""Exception types for the ingestion core pipeline (persistence and configuration failures).

Adapter-level fetch and parse failures have their own types in
`ps_service.ingestion.adapters.errors`.
"""

from __future__ import annotations


class IngestionPersistenceError(Exception):
    """The structural graph or RegulatoryInstrument node could not be persisted or verified.

    Raised by `graph_writer.build_native_writes` when the structure cannot be fully built (an
    unknown element type, or a property the gateway cannot store; nothing is written in that
    case), by `graph_submission.submit_or_raise` when the Graph Write Gateway refuses the group,
    cannot reach its stores, or commits it without being able to apply it yet, and by
    `graph_writer.verify_structural_graph_reachable` when the persisted structure can't be
    verified. Callers should treat it as a failed run: no partial structure is left committed by
    the writer, and the run can be retried once the cause is fixed.
    """


class IngestionConfigurationError(Exception):
    """Ingestion's FalkorDB connection could not be established.

    Raised when the host and port resolved from `ServiceConfig` are unreachable or the connection
    cannot be validated. Callers should fix the FalkorDB configuration or bring the database up
    before retrying.
    """
