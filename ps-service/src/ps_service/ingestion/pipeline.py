"""The ingestion entry point: fetch, register, persist and verify one regulation.

`ingest_regulatory_instrument` runs the fetch, register, persist, verify sequence that is
Ingestion's whole job for one regulation-ingestion run.

Every run is wrapped in one `bind_run_context(run_id)` block, so each `emit_log_entry` call inside
it carries that run id through the `run_context` ContextVar and the id is never passed
explicitly. When the caller passes no `run_id`, a fresh uuid4 is minted, so two calls log two
distinct run ids; an API caller may pass its request-scoped `run_id` to correlate these entries
with the wider request.

The module is regulation-independent: `short_name` (for example "CRA") and `version` (for
example "1.0") are caller-supplied and never derived from `structure.metadata.title` or by
branching on `identifier`'s value. Its executable code names no specific regulation.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ps_service.ingestion.errors import IngestionPersistenceError
from ps_service.ingestion.falkordb_client import native_graph_name
from ps_service.ingestion.graph_submission import (
    require_caught_up,
    stamp_legacy_edges_before_first_write,
    submit_or_raise,
)
from ps_service.ingestion.graph_writer import (
    build_native_group,
    build_native_writes,
    read_existing_nodes,
    verify_structural_graph_reachable,
)
from ps_service.ingestion.models import IngestResult
from ps_service.logging import LogEmitter, bind_run_context, emit_log_entry

if TYPE_CHECKING:
    from ps_service.ingestion.adapters.base import IngestionAdapter
    from ps_service.ingestion.falkordb_client import GraphHandle
    from ps_service.ingestion.graph_submission import GroupSubmitter

_COMPONENT = "ingestion"


def ingest_regulatory_instrument(  # noqa: PLR0913 -- the stage's call shape: identity, adapter, read handle, gateway seam and run context; no natural grouping
    identifier: str,
    short_name: str,
    *,
    version: str,
    adapter: IngestionAdapter,
    graph: GraphHandle,
    submitter: GroupSubmitter,
    audit_event_id: str,
    run_id: str | None = None,
    emitter: LogEmitter | None = None,
) -> IngestResult:
    """Ingest one regulation end to end: fetch, register, persist, verify.

    Runs, in order: `adapter.fetch_regulatory_instrument_structure(identifier)`; build and
    validate the native graph's primitives; stamp identity onto any legacy identity-less edges of
    the target graph; submit the primitives as one group through the Graph Write Gateway (the
    RegulatoryInstrument, the structural nodes, then the `HAS` edges, logged under
    `audit_event_id`); require the graph to be caught up with its log; then
    `verify_structural_graph_reachable`. One log entry is emitted per completed stage
    (`fetch_regulatory_instrument_structure`, `register_regulatory_instrument_version` after the
    primitives are built, `persist_native_structural_graph` after the group is applied,
    `verify_structural_graph_reachable`).

    Business Rules:
        The RegulatoryInstrument id is `f"{short_name}-{version}"` (for example "CRA-1.0"),
        never derived from the fetched document or from `identifier`, which keeps this function
        regulation-independent.

    Args:
        identifier: Source key understood by `adapter` (a CELEX number for `CellarEliAdapter`).
        short_name: Caller-supplied short name of the regulation; also selects the native graph
            name (`native_graph_name`).
        version: Caller-supplied version label combined with `short_name` into the instrument id.
        adapter: Fetches and parses the instrument's structure.
        graph: Read handle for the native graph; it is only read (existence of edge endpoints
            outside the group, and the reachability check).
        submitter: Graph Write Gateway seam through which all writes are made.
        audit_event_id: Audit event the submitted group and any legacy-edge stamp are logged
            under.
        run_id: Request-scoped run id to bind; None mints a fresh uuid4.
        emitter: Log emitter for the stage entries; None uses the default emitter.

    Returns:
        An `IngestResult` with the instrument id, the bound run id and the per-label
        `ReachabilityCount` from the final check.

    Side Effects:
        Fetches the instrument from the source, submits one mutation group (and possibly one
        legacy-edge stamp group) to the graph write log and graph, reads FalkorDB, and emits one
        log entry per completed stage.

    Raises:
        CellarFetchError: Raised by the Cellar/ELI adapter when the fetch fails.
        CellarParseError: Raised by the Cellar/ELI adapter when the markup cannot be parsed.
        IngestionPersistenceError: A structure element is invalid, the gateway refused the group
            or could not apply it yet, or the reachability check found an orphan.

    No other error handling is added; a failure at any stage aborts the run and no later stage
    emits a log entry.
    """
    with bind_run_context(run_id) as bound_run_id:
        regulatory_instrument_id = f"{short_name}-{version}"

        structure = adapter.fetch_regulatory_instrument_structure(identifier)
        _emit(
            component=_COMPONENT,
            action="fetch_regulatory_instrument_structure",
            regulatory_instrument_id=regulatory_instrument_id,
            emitter=emitter,
        )

        writes = build_native_writes(
            regulatory_instrument_id, structure.metadata, structure.nodes, structure.edges
        )
        _emit(
            component=_COMPONENT,
            action="register_regulatory_instrument_version",
            regulatory_instrument_id=regulatory_instrument_id,
            emitter=emitter,
        )

        graph_name = native_graph_name(short_name)
        group = build_native_group(
            writes,
            existing=read_existing_nodes(graph, writes),
            graph_name=graph_name,
            audit_event_id=audit_event_id,
            checkpoint_requested=True,
        )
        stamp_legacy_edges_before_first_write(
            submitter, graph_name, audit_event_id, IngestionPersistenceError
        )
        submit_or_raise(submitter, group, IngestionPersistenceError)
        require_caught_up(submitter, graph_name, IngestionPersistenceError)
        _emit(
            component=_COMPONENT,
            action="persist_native_structural_graph",
            regulatory_instrument_id=regulatory_instrument_id,
            emitter=emitter,
        )

        counts = verify_structural_graph_reachable(graph, regulatory_instrument_id)
        _emit(
            component=_COMPONENT,
            action="verify_structural_graph_reachable",
            regulatory_instrument_id=regulatory_instrument_id,
            emitter=emitter,
        )

        return IngestResult(
            regulatory_instrument_id=regulatory_instrument_id, run_id=bound_run_id, counts=counts
        )


def _emit(
    *, component: str, action: str, regulatory_instrument_id: str, emitter: LogEmitter | None
) -> None:
    """Emit one succeeded-stage log entry; `emit_log_entry` adds the bound run id itself."""
    emit_log_entry(
        component=component,
        action=action,
        entity_id=regulatory_instrument_id,
        outcome="succeeded",
        emitter=emitter,
    )
