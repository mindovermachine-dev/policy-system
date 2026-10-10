"""Apply decoded log entries to FalkorDB in batched, order-preserving runs (issue #206).

Before the first load of a pass, the `id` indexes of the labels it writes are ensured (see
`index_manager`). Entries then apply in position order. A run is a maximal stretch of consecutive
entries of the same operation (App-E: `_runs`); each run becomes `ceil(n / batch_size)`
parameterized queries per label and the caller is told after each completed run, so the applied
marker can follow.
"""

from __future__ import annotations

from itertools import groupby
from typing import TYPE_CHECKING

from ps_service.graph_gateway.cypher import (
    delete_edge_query,
    delete_edge_rows,
    delete_node_query,
    merge_property_query,
    merge_property_rows,
    node_id_rows,
    remove_property_query,
    upsert_edge_query,
    upsert_edge_rows,
    upsert_node_query,
    upsert_node_rows,
)
from ps_service.graph_gateway.entry_codec import decode_entry
from ps_service.graph_gateway.index_manager import IdIndexes
from ps_service.graph_gateway.models import (
    DeleteEdge,
    DeleteNode,
    MergeProperty,
    RemoveProperty,
    UpsertEdge,
    UpsertNode,
)
from ps_service.graph_gateway.validation import node_labels

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Sequence

    from ps_service.graph_gateway.models import GraphLogEntry, Primitive
    from ps_service.ingestion.falkordb_client import GraphHandle


type _Positioned = tuple[int, Primitive]
"""A decoded primitive with its log position."""


def apply_entries(
    graph: GraphHandle,
    entries: Sequence[GraphLogEntry],
    *,
    batch_size: int,
    on_run_applied: Callable[[int], None],
) -> None:
    """Apply `entries` to `graph`, calling `on_run_applied(last_position)` after each run."""
    apply_decoded(
        graph,
        [(entry.position, decode_entry(entry)) for entry in entries],
        batch_size=batch_size,
        on_run_applied=on_run_applied,
    )


def apply_decoded(
    graph: GraphHandle,
    decoded: Sequence[tuple[int, Primitive]],
    *,
    batch_size: int,
    on_run_applied: Callable[[int], None],
    indexes: IdIndexes | None = None,
) -> None:
    """Apply already decoded `(position, primitive)` pairs, in order (see `apply_entries`).

    `indexes` carries what is known of the graph's `id` indexes across calls (a replay passes one
    for all its pages); without it this call lists the indexes itself.
    """
    (indexes if indexes is not None else IdIndexes(graph)).ensure(
        label for _, p in decoded for label in node_labels(p)
    )
    for run in _runs(decoded):
        for chunk in _chunks_of(run, batch_size):
            _apply_chunk(graph, [primitive for _, primitive in chunk])
        on_run_applied(run[-1][0])


def _runs(decoded: Sequence[_Positioned]) -> Iterator[list[_Positioned]]:
    """Cut `decoded` into runs: consecutive entries of the same operation (App-E).

    A run of node upserts is cut again where a node repeats, so that a later row for a node
    always lands in a later run.
    """
    for _, members in groupby(decoded, key=lambda item: _run_key(item[1])):
        run = list(members)
        if isinstance(run[0][1], UpsertNode):
            yield from _split_at_repeated_nodes(run)
        else:
            yield run


def _split_at_repeated_nodes(run: list[_Positioned]) -> Iterator[list[_Positioned]]:
    """Cut a run of node upserts before the first row that repeats a node of the current part."""
    part: list[_Positioned] = []
    seen: set[tuple[str, str]] = set()
    for item in run:
        node = _node_of(item[1])
        if node in seen:
            yield part
            part, seen = [], set()
        part.append(item)
        seen.add(node)
    yield part


def _chunks_of(run: list[_Positioned], batch_size: int) -> Iterator[list[_Positioned]]:
    """Cut a run into batches of one query each: grouped by label, `batch_size` rows at most."""
    by_label: dict[str, list[_Positioned]] = {}
    for item in run:
        by_label.setdefault(_label_of(item[1]), []).append(item)
    for members in by_label.values():
        for start in range(0, len(members), batch_size):
            yield members[start : start + batch_size]


def _node_of(primitive: Primitive) -> tuple[str, str]:
    """Return `(label, id)` of a node upsert (the only primitive a split run holds)."""
    if not isinstance(primitive, UpsertNode):
        message = "only node upserts are split at a repeated node"
        raise TypeError(message)
    return (primitive.label, primitive.id)


def _label_of(primitive: Primitive) -> str:
    """Name the label a query of a run is bound to: only node upserts mix labels in one run."""
    return primitive.label if isinstance(primitive, UpsertNode) else ""


def _run_key(primitive: Primitive) -> tuple[str, ...]:
    """Name the run a primitive belongs to.

    Node upserts of any label share a run (they are regrouped by label per query); every other
    operation runs with the same labels, type or key tuple.
    """
    match primitive:
        case UpsertNode():
            return (primitive.op,)
        case MergeProperty() | DeleteNode():
            return (primitive.op, primitive.label)
        case RemoveProperty():
            return (primitive.op, primitive.label, *primitive.keys)
        case UpsertEdge() | DeleteEdge():
            return (primitive.op, primitive.type, primitive.source.label, primitive.target.label)


def _only[P](chunk: Sequence[Primitive], kind: type[P]) -> list[P]:
    """Narrow a chunk to its members of `kind` (a run is homogeneous, so that is all of them)."""
    return [primitive for primitive in chunk if isinstance(primitive, kind)]


def _apply_chunk(graph: GraphHandle, chunk: Sequence[Primitive]) -> None:
    """Send one batch of a single run as a single query."""
    first = chunk[0]
    match first:
        case UpsertNode():
            rows = upsert_node_rows(_only(chunk, UpsertNode))
            graph.query(upsert_node_query(first.label), {"rows": rows})
        case MergeProperty():
            rows = merge_property_rows(_only(chunk, MergeProperty))
            graph.query(merge_property_query(first.label), {"rows": rows})
        case RemoveProperty():
            rows = node_id_rows(_only(chunk, RemoveProperty))
            graph.query(remove_property_query(first.label, first.keys), {"rows": rows})
        case DeleteNode():
            rows = node_id_rows(_only(chunk, DeleteNode))
            graph.query(delete_node_query(first.label), {"rows": rows})
        case UpsertEdge():
            rows = upsert_edge_rows(_only(chunk, UpsertEdge))
            graph.query(upsert_edge_query(first), {"rows": rows})
        case DeleteEdge():
            rows = delete_edge_rows(_only(chunk, DeleteEdge))
            graph.query(delete_edge_query(first), {"rows": rows})
