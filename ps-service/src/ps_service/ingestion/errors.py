"""Domain-specific exception types for ps_service.ingestion's core pipeline.

Covers persistence and configuration. Adapter-level fetch/parse failures
have their own types — see `ps_service.ingestion.adapters.errors`.
"""

from __future__ import annotations


class IngestionPersistenceError(Exception):
    """A FalkorDB write for the structural graph or RegulatoryInstrument node failed.

    Raised by `graph_writer.register_regulatory_instrument_version` when required
    metadata is missing (CA doc: "Reject with a clear error if required
    properties are missing... no partial node") and by
    `graph_writer.persist_native_structural_graph`/
    `verify_structural_graph_reachable` when the structure can't be fully
    persisted or verified (CA doc: "Abort with no partial write").
    """


class IngestionConfigurationError(Exception):
    """Ingestion's FalkorDB connection could not be established.

    The resolved `ServiceConfig` gave an unreachable host/port, or the
    connection could not be validated — raised by
    `falkordb_client.connect`/`connect_from_config`.
    """
