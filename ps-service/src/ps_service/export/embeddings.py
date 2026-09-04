"""ps_service.export.embeddings -- D7's Capability/Policy embedding backfill.

`backfill_capability_embeddings` computes and persists an embedding on every
exported Capability (and, for `internal` sources, Policy) node that doesn't
already carry one -- a new, one-time `RouteEmbedding` call per canonical
node, paid at curation time only (PLAN.md D7). Writes land on the live
source instance's `{short}_baseline` graph (`SET n.embedding = $vector`,
one Cypher write per node) -- a deliberate, documented side effect of
running Export, not of any customer-facing action -- so that restore never
needs a live LLM provider (AC-BI-005/AC-BI-006).

Called by `ps_service.export.export_instrument.export_instrument` before
either graph is serialized (D7's own ordering: embed, then dump/serialize).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal, Protocol, cast

from ps_service.llm_interface.embedding import route_embedding

if TYPE_CHECKING:
    from ps_service.export.falkordb_connection import (
        _GraphQueryHandle,  # pyright: ignore[reportPrivateUsage]
    )
    from ps_service.llm_interface.client import EmbeddingCaller
    from ps_service.logging.emitter import LogEmitter

__all__ = ["backfill_capability_embeddings"]

# The property this Capability/Policy node's embedding text is computed from --
# Capability has no `title`, Policy has no `name` (docs/artifacts/ps-domain-concepts.md's
# own Properties tables), so this mapping is a fixed, per-label literal, never adapter-sourced.
_TEXT_PROPERTY_BY_LABEL: dict[str, str] = {"Capability": "name", "Policy": "title"}

# Which labels D7 backfills, keyed by `source_type` -- an `external` source's ingestion
# pipeline always stops at Capability (docs/artifacts/ps-domain-concepts.md:44); only an
# `internal` source's Domain Mapping Adapter reaches Policy.
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


def _write_embedding(
    graph: _GraphQueryHandle, label: str, node_id: str, embedding: list[float]
) -> None:
    graph.query(
        f"MATCH (n:{label} {{id: $id}}) SET n.embedding = $embedding",
        {"id": node_id, "embedding": embedding},
    )


def backfill_capability_embeddings(
    baseline_graph: _GraphQueryHandle,
    *,
    source_type: Literal["external", "internal"],
    model: str,
    call_embedding: EmbeddingCaller | None = None,
    emitter: LogEmitter | None = None,
) -> int:
    """Compute and persist an embedding on every embeddable node lacking one.

    Reads every Capability (and, for `source_type="internal"`, Policy) node
    in `baseline_graph` whose `embedding` property is not yet set, calls
    `RouteEmbedding` once per node on its own name/title text, and writes
    the resulting vector back onto that node (D7). Caller has already
    selected `baseline_graph` (mirrors `export.serialize.serialize_graph`'s
    own "caller selects, this function doesn't" convention).

    Returns the total count of nodes written -- callers use this only for
    observability, never as a correctness signal.
    """
    written = 0
    for label in _EMBEDDABLE_LABELS_BY_SOURCE_TYPE[source_type]:
        for node_id, text in _read_nodes_missing_embedding(baseline_graph, label):
            result = route_embedding(
                text, model=model, call_embedding=call_embedding, emitter=emitter
            )
            _write_embedding(baseline_graph, label, node_id, result.vector)
            written += 1
    return written
