"""ps_service.export.embeddings -- D7's Capability/Policy embedding backfill.

`backfill_capability_embeddings` computes and persists an embedding on every
exported Capability (and, for `internal` sources, Policy) node that doesn't
already carry one -- a new, one-time `RouteEmbedding` call per canonical
node, paid at curation time only (PLAN.md D7). Since issue #213 the write goes through the
Graph Write Gateway: every embedding is computed first, then ONE group of embedding-only
`UpsertNode` primitives is submitted on the live source instance's `{short}_baseline` graph,
anchored to the system audit row Export recorded -- a deliberate, documented side effect of
running Export, not of any customer-facing action -- so that restore never needs a live LLM
provider (AC-BI-005/AC-BI-006). If no node lacks an embedding nothing is submitted, so a re-run
adds no log entry; if an embedding call fails nothing has been written.

Called by `ps_service.export.export_instrument.export_instrument` before
either graph is serialized (D7's own ordering: embed, then dump/serialize).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal, Protocol, cast

from pydantic import ValidationError

from ps_service.export.errors import ExportGraphWriteError
from ps_service.graph_gateway.models import MutationGroup, UpsertNode
from ps_service.ingestion.graph_submission import require_caught_up, submit_or_raise
from ps_service.llm_interface.embedding import route_embedding
from ps_service.logging import emit_log_entry

if TYPE_CHECKING:
    from ps_service.export.falkordb_connection import (
        _GraphQueryHandle,  # pyright: ignore[reportPrivateUsage]
    )
    from ps_service.ingestion.graph_submission import GroupSubmitter
    from ps_service.llm_interface.client import EmbeddingCaller
    from ps_service.logging.emitter import LogEmitter

__all__ = ["backfill_capability_embeddings"]

_COMPONENT = "export"
_ACTION = "backfill_embeddings"

# The property this Capability/Policy node's embedding text is computed from --
# Capability has no `title`, Policy has no `name` (docs/artifacts/ps-domain-concepts.md's
# own Properties tables), so this mapping is a fixed, per-label literal, never adapter-sourced.
_TEXT_PROPERTY_BY_LABEL: dict[str, str] = {"Capability": "name", "Policy": "title"}

# Which labels D7 backfills, keyed by `source_type` -- an `external` source's ingestion
# pipeline always stops at Capability (docs/artifacts/ps-domain-concepts.md:44); both
# Capability (all sources) and Policy (internal-SoP-derived instances, authored via
# Ingestion's internal-seed adapter) get embeddings backfilled here.
_EMBEDDABLE_LABELS_BY_SOURCE_TYPE: dict[Literal["external", "internal"], tuple[str, ...]] = {
    "external": ("Capability",),
    "internal": ("Capability", "Policy"),
}


class _QueryResult(Protocol):
    """Structural stand-in for `falkordb.QueryResult` -- the one field this module reads."""

    @property
    def result_set(self) -> list[object]: ...


def _rows(
    graph: _GraphQueryHandle, query: str, params: dict[str, object] | None = None
) -> list[list[object]]:
    """The one call site every `graph.query()` read in this module goes through."""
    result = graph.query(query, params)
    return cast("list[list[object]]", cast("_QueryResult", result).result_set)


def _read_nodes_missing_embedding(graph: _GraphQueryHandle, label: str) -> list[tuple[str, str]]:
    text_property = _TEXT_PROPERTY_BY_LABEL[label]
    query = f"MATCH (n:{label}) WHERE n.embedding IS NULL RETURN n.id, n.{text_property}"
    return [(cast("str", row[0]), cast("str", row[1])) for row in _rows(graph, query)]


def _build_upserts(computed: list[tuple[str, str, list[float]]]) -> tuple[UpsertNode, ...]:
    """One embedding-only `UpsertNode` per computed `(label, node id, vector)`, in order.

    Raises:
        ExportGraphWriteError: a vector the gateway cannot store (empty or non-finite).
    """
    try:
        return tuple(
            UpsertNode(label=label, id=node_id, embedding=tuple(vector))
            for label, node_id, vector in computed
        )
    except ValidationError as exc:
        message = "an embedding holds a value the graph cannot store"
        raise ExportGraphWriteError(message) from exc


def backfill_capability_embeddings(
    baseline_graph: _GraphQueryHandle,
    *,
    baseline_graph_name: str,
    submitter: GroupSubmitter,
    audit_event_id: str,
    source_type: Literal["external", "internal"],
    model: str,
    call_embedding: EmbeddingCaller | None = None,
    emitter: LogEmitter | None = None,
) -> int:
    """Compute and persist an embedding on every embeddable node lacking one.

    Order: the graph must hold everything committed to its log (an earlier pending group would
    make the read below stale); read every Capability (and, for `source_type="internal"`, Policy)
    node whose `embedding` property is not yet set; call `RouteEmbedding` once per node on its
    own name/title text -- ALL of them, before anything is submitted, so a failure here has
    written nothing; then submit ONE `baseline_graph_name` group of embedding-only `UpsertNode`
    primitives anchored to `audit_event_id` (D7). With nothing to embed no group is submitted.
    Caller has already selected `baseline_graph` (mirrors `export.serialize.serialize_graph`'s own
    "caller selects, this function doesn't" convention).

    Returns the total count of nodes written -- callers use this only for
    observability, never as a correctness signal.

    Raises:
        ExportGraphWriteError: the graph is behind its log, the gateway refused the group or could
            not reach its stores, the group was committed but not applied, or an embedding cannot
            be stored.
    """
    require_caught_up(submitter, baseline_graph_name, ExportGraphWriteError)
    computed: list[tuple[str, str, list[float]]] = []
    for label in _EMBEDDABLE_LABELS_BY_SOURCE_TYPE[source_type]:
        for node_id, text in _read_nodes_missing_embedding(baseline_graph, label):
            result = route_embedding(
                text, model=model, call_embedding=call_embedding, emitter=emitter
            )
            computed.append((label, node_id, result.vector))
    primitives = _build_upserts(computed)
    if not primitives:
        emit_log_entry(
            component=_COMPONENT,
            action=_ACTION,
            entity_id=baseline_graph_name,
            outcome="unchanged",
            extra={"graph": baseline_graph_name, "nodes": 0},
            emitter=emitter,
        )
        return 0
    group = MutationGroup(
        graph=baseline_graph_name, audit_event_id=audit_event_id, primitives=primitives
    )
    try:
        submit_or_raise(submitter, group, ExportGraphWriteError)
        require_caught_up(submitter, baseline_graph_name, ExportGraphWriteError)
    except ExportGraphWriteError as exc:
        emit_log_entry(
            component=_COMPONENT,
            action=_ACTION,
            entity_id=baseline_graph_name,
            outcome="failed",
            extra={
                "graph": baseline_graph_name,
                "nodes": len(primitives),
                "error_type": type(exc.__cause__).__name__
                if exc.__cause__ is not None
                else "NotApplied",
            },
            emitter=emitter,
        )
        raise
    emit_log_entry(
        component=_COMPONENT,
        action=_ACTION,
        entity_id=baseline_graph_name,
        outcome="submitted",
        extra={"graph": baseline_graph_name, "nodes": len(primitives)},
        emitter=emitter,
    )
    return len(primitives)
