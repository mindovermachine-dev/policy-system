"""Backfill graph embeddings on exported Role, Capability and Policy nodes.

`backfill_capability_embeddings` computes and stores an embedding on every Role and Capability
node (and, for `internal` sources, every Policy node) of the baseline graph that does not already
carry one. The cost of embedding calls is paid once, at curation time, so restoring an
instrument never needs a live LLM provider. All embeddings are computed first, then one group
of embedding-only `UpsertNode` primitives is submitted through the Graph Write Gateway on the
source instance's baseline graph, anchored to the audit row that Export recorded. If no node
lacks an embedding nothing is submitted, so a re-run adds no log entry; if an embedding call
fails nothing has been written.

`ps_service.export.export_instrument.export_instrument` calls it before either graph is
serialized, so the exported artifacts include the embeddings.
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

# The property each label's embedding text is computed from: Role and Capability have no `title`
# and Policy has no `name` (see docs/artifacts/ps-domain-concepts.md), so the mapping is a fixed
# per-label literal, never adapter-sourced.
_TEXT_PROPERTY_BY_LABEL: dict[str, str] = {"Role": "name", "Capability": "name", "Policy": "title"}

# Labels that receive embeddings, keyed by `source_type`: an `external` source's ingestion
# pipeline stops at Capability, while an `internal` source also carries Policy nodes (authored
# through Ingestion's internal-seed adapter). Role names are embedded for both, so a restore can
# score Role near misses without a live embedding provider.
_EMBEDDABLE_LABELS_BY_SOURCE_TYPE: dict[Literal["external", "internal"], tuple[str, ...]] = {
    "external": ("Role", "Capability"),
    "internal": ("Role", "Capability", "Policy"),
}


class _QueryResult(Protocol):
    """Structural stand-in for `falkordb.QueryResult`, limited to the field this module reads."""

    @property
    def result_set(self) -> list[object]: ...


def _rows(
    graph: _GraphQueryHandle, query: str, params: dict[str, object] | None = None
) -> list[list[object]]:
    """Run a read query on `graph` and return its result rows (single call site for reads)."""
    result = graph.query(query, params)
    return cast("list[list[object]]", cast("_QueryResult", result).result_set)


def _read_nodes_missing_embedding(graph: _GraphQueryHandle, label: str) -> list[tuple[str, str]]:
    """Return `(node id, embedding text)` for every `label` node whose `embedding` is unset."""
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
    """Compute and store an embedding on every embeddable node that lacks one.

    The baseline graph is first required to hold everything committed to its log, because an
    earlier pending group would make the read below stale. Every Role and Capability node (and, for
    `source_type="internal"`, every Policy node) whose `embedding` property is unset is then
    read, and one embedding is computed per node from its `name` (Role, Capability) or `title`
    (Policy). All embeddings are computed before anything is submitted, so a failed embedding
    call has written nothing. Finally one group of embedding-only `UpsertNode` primitives is
    submitted on `baseline_graph_name` and the graph is required to have applied it. With
    nothing to embed no group is submitted.

    Args:
        baseline_graph: Read handle on the already-selected baseline graph; the caller picks the
            graph, this function does not.
        baseline_graph_name: Name of that graph, used as the target of the submitted group and
            for the caught-up checks. Non-empty.
        submitter: Graph Write Gateway submitter used for the group and the caught-up checks.
        audit_event_id: Id of the audit row Export recorded; the group is anchored to it.
        source_type: `"external"` backfills Role and Capability nodes; `"internal"` backfills
            Role, Capability and Policy nodes.
        model: Embedding model name passed to `RouteEmbedding`.
        call_embedding: Embedding provider callable; `None` uses the default provider caller.
        emitter: Log emitter for the outcome entry and the embedding-call entries; `None` uses
            the default emitter.

    Returns:
        The number of nodes whose embedding was written; `0` when every node already had one.
        Callers use it for observability only, not as a correctness signal.

    Raises:
        ExportGraphWriteError: The graph is behind its log, the gateway refused the group or
            could not reach its stores, the group was committed but not applied, or an embedding
            holds a value the graph cannot store (empty or non-finite).
        LlmProviderError: An embedding call failed or returned no usable vector; nothing has
            been written.

    Side Effects:
        One `RouteEmbedding` call per node lacking an embedding, one gateway group submission
        on `baseline_graph_name` when at least one node needs an embedding, and one log entry
        with outcome `unchanged`, `submitted` or `failed`.
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
