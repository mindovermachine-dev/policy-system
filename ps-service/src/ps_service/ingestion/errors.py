"""Domain-specific exception types for ps_service.ingestion's core pipeline.

Covers persistence and configuration. Adapter-level fetch/parse failures
have their own types — see `ps_service.ingestion.adapters.errors`.
"""

from __future__ import annotations


class IngestionPersistenceError(Exception):
    """The structural graph or RegulatoryInstrument node could not be persisted or verified.

    Raised by `graph_writer.build_native_writes` when the structure can't be fully built (an
    unknown element type, or a property the gateway cannot store; CA doc: "Abort with no partial
    write"), by `graph_submission.submit_or_raise` when the Graph Write Gateway refuses the group,
    cannot reach its stores, or commits it without being able to apply it yet, and by
    `graph_writer.verify_structural_graph_reachable` when the persisted structure can't be
    verified.
    """


class IngestionConfigurationError(Exception):
    """Ingestion's FalkorDB connection could not be established.

    The resolved `ServiceConfig` gave an unreachable host/port, or the
    connection could not be validated — raised by
    `falkordb_client.connect`/`connect_from_config`.
    """
