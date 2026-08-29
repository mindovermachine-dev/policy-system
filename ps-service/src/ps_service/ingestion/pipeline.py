"""ps_service.ingestion pipeline — the primary-use-case entry point.

Implements PLAN_REVIEWED.md §7 Increment 11: `ingest_regulatory_instrument()` wires
Increments 1-10 (adapter Protocol, FalkorDB persistence) into the
fetch -> register -> persist -> verify sequence that is Ingestion's whole
job for one regulation-ingestion run (CA doc's `FetchRegulatoryInstrumentStructure` /
`RegisterRegulatoryInstrumentVersion` / `PersistNativeStructuralGraph` actions, plus
this component's own AC-004 reachability check).

AC-005 (structured logging correlation, closing CA doc line 618's flagged
gap): this is the first real caller of `bind_run_context()` for Ingestion.
One `with bind_run_context() as run_id:` block wraps the whole pipeline;
every `emit_log_entry(...)` call inside it auto-bakes that run_id via the
`run_context` ContextVar mechanism (never passed explicitly — see
`ps_service.llm_interface._logging_support.log`'s identical precedent).
Two separate `ingest_regulatory_instrument()` calls therefore carry two distinct
run_ids in their emitted log entries.

AC-006 (regulation-independence, verifiable at the code level): `short_name`
(e.g. "CRA") and `version` (e.g. "1.0") are CALLER-SUPPLIED parameters —
never derived by inspecting `structure.metadata.title` or branching on
`identifier`'s value. This file contains no `if`/`elif`/comparison naming a
specific regulation anywhere in its executable code. The only place
"CRA"/"GDPR"/"NIS2" appear in this module is this docstring's illustrative
prose, right here — `tests/ingestion/test_pipeline.py`'s AST-based scan
explicitly excludes docstring nodes from its check, so naming regulations
here as examples does not trip it; only executable if/elif/comparison logic
naming a regulation would.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ps_service.ingestion.graph_writer import (
    persist_native_structural_graph,
    register_regulatory_instrument_version,
    verify_structural_graph_reachable,
)
from ps_service.ingestion.models import IngestResult
from ps_service.logging import LogEmitter, bind_run_context, emit_log_entry

if TYPE_CHECKING:
    from ps_service.ingestion.adapters.base import IngestionAdapter
    from ps_service.ingestion.falkordb_client import GraphHandle

_COMPONENT = "ingestion"


def ingest_regulatory_instrument(
    identifier: str,
    short_name: str,
    *,
    version: str,
    adapter: IngestionAdapter,
    graph: GraphHandle,
    emitter: LogEmitter | None = None,
) -> IngestResult:
    """Ingest one regulation end to end: fetch, register, persist, verify.

    The primary-use-case entry point (UC-1's manual trigger; UC-4's
    TriggerReingestion calls this too, later — out of scope here). Binds a
    fresh run_id for the whole call (AC-005), then runs, in order:
    `adapter.fetch_regulatory_instrument_structure(identifier)` ->
    `register_regulatory_instrument_version` -> `persist_native_structural_graph` ->
    `verify_structural_graph_reachable`, emitting one log entry per
    completed stage. `identifier` is whatever the injected `adapter`
    expects (a CELEX number for `CellarEliAdapter`); `short_name`/`version`
    are caller-supplied and combined into the RegulatoryInstrument's natural-key id
    (`f"{short_name}-{version}"`, e.g. "CRA-1.0") — never derived from the
    fetched document or from `identifier`'s value, which is what keeps this
    function regulation-independent (AC-006).

    Raises whatever the underlying adapter/`graph_writer` calls raise
    (`CellarFetchError`/`CellarParseError` from the adapter,
    `IngestionPersistenceError` from `graph_writer`) — this function adds no
    error handling of its own; a failure at any stage aborts the run with no
    further stage's log entry emitted.
    """
    with bind_run_context() as run_id:
        regulatory_instrument_id = f"{short_name}-{version}"

        structure = adapter.fetch_regulatory_instrument_structure(identifier)
        _emit(
            component=_COMPONENT,
            action="fetch_regulatory_instrument_structure",
            regulatory_instrument_id=regulatory_instrument_id,
            emitter=emitter,
        )

        register_regulatory_instrument_version(graph, regulatory_instrument_id, structure.metadata)
        _emit(
            component=_COMPONENT,
            action="register_regulatory_instrument_version",
            regulatory_instrument_id=regulatory_instrument_id,
            emitter=emitter,
        )

        persist_native_structural_graph(
            graph, regulatory_instrument_id, structure.nodes, structure.edges
        )
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
            regulatory_instrument_id=regulatory_instrument_id, run_id=run_id, counts=counts
        )


def _emit(
    *, component: str, action: str, regulatory_instrument_id: str, emitter: LogEmitter | None
) -> None:
    """Emit one completed-stage log entry.

    `run_id` is never passed explicitly — `emit_log_entry` auto-bakes the
    currently bound run context (AC-005's mechanism; mirrors
    `ps_service.llm_interface._logging_support.log`).
    """
    emit_log_entry(
        component=component,
        action=action,
        entity_id=regulatory_instrument_id,
        outcome="succeeded",
        emitter=emitter,
    )
