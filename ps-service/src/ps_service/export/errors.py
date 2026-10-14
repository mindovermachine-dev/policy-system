"""Domain-specific exception types for `ps_service.export`.

One exception type per distinct failure boundary this component owns, so callers never have
to catch a generic `Exception` or `ValueError`.
"""

from __future__ import annotations


class ExportSourceGraphError(Exception):
    """A source graph's shape is not exportable by `serialize.serialize_graph`.

    Raised when any node, or any edge endpoint, carries zero or more than one label. Every
    writer in this codebase creates single-labeled nodes, so this signals a graph that breaks
    the contract; it is never coerced by picking one label. The caller should treat the source
    graph as corrupt and stop the export; retrying does not help.
    """


class ExportInstrumentIdMismatchError(Exception):
    """The requested `instrument_id` differs from the baseline graph's `RegulatoryInstrument.id`.

    Restore (`ps_service.restore.restore_instrument`) requires a manifest's `instrument_id` to
    equal the artifact's own `RegulatoryInstrument.id`, so a mismatch would produce an artifact
    that restore rejects with a `content_validation` failure. Export raises this before writing
    anything to disk. The caller should pass the id the baseline graph actually holds.
    """


class ExportGraphWriteError(Exception):
    """The embedding backfill's gateway group did not take effect.

    Raised when the baseline graph is behind its log, the Graph Write Gateway refused the group
    or could not reach Postgres or FalkorDB, the group was committed but could not be applied yet,
    or an embedding holds a value the graph cannot store. The message is the gateway's sanitized
    text (`FalkorDB write failed: ...`) and the gateway error, if any, is the `__cause__`. Nothing
    is lost on a pending apply: the group is in the log, the gateway applies it on recovery and a
    re-run is idempotent (a node that now has an embedding is not embedded again). The caller
    should re-run the export once the gateway stores are reachable.
    """
