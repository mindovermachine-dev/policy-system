"""The native structural graph of one external ingestion: what is written, and how it is checked.

Nothing here writes to FalkorDB. The module builds the primitives the Graph Write Gateway applies
(`pipeline.ingest_regulatory_instrument` submits them) and reads the graph to check the result:

- `build_native_writes` validates and builds the RegulatoryInstrument node, the structural nodes
  and the `HAS` edges. It validates every node and edge `element_type` against
  `_KNOWN_ELEMENT_TYPES` in a whole-collection first pass and raises `IngestionPersistenceError`
  on the first violation, before a single primitive exists. The whole graph is one group that the
  gateway commits or rejects as a whole, so a large regulation is never partially written.
- `read_existing_nodes` and `build_native_group`: an edge whose endpoint is neither in the group
  nor already in the graph is dropped silently, because the gateway would reject the whole group
  for it. One batched read per label is issued once, immediately before the submit; a node
  deleted in between makes the gateway reject the group, which surfaces as the persistence error.
- `verify_structural_graph_reachable` checks that, for every structural label, no node is an
  orphan, meaning unreachable from every RegulatoryInstrument node via `HAS` at any depth. It is
  version-agnostic, because a `{short}_native` graph can hold several coexisting versions'
  subtrees after a re-ingest. It raises `IngestionPersistenceError`, not a warning, on any orphan.
  It reads the live graph, so the caller runs it only once the graph holds everything committed
  (`graph_submission.require_caught_up`).

Structural node and edge ids are used exactly as the Cellar/ELI adapter produces them
(CELEX-prefixed, for example `"32024R2847#art_1"`) and are never rewritten here. One FalkorDB
graph per regulation (`falkordb_client.native_graph_name`) makes those ids unique within their
graph and stable across re-ingest, so the reachability and count checks are traversal- and
count-based, not id-string-based.

The adapter never sees the final `{SHORT}-{VERSION}` regulation id, only the CELEX identifier it
was called with. Every `StructuralEdge` with `parent_element_type == "RegulatoryInstrument"`
therefore carries that CELEX as a placeholder `parent_id`. `_has_edge` substitutes the
caller-supplied `regulatory_instrument_id` for it and uses `edge.parent_id` unchanged for every
other edge. Without the substitution, top-level edges would name a node that does not exist and
everything beneath them would be unreachable.

Every edge carries an `identity`, `HAS:<parent>-><child>`, which is the gateway's merge key. A node
property the gateway reserves (`id`, `embedding`) or cannot store (a non-finite number) is
rejected instead of silently rewriting the node id.

Query safety: `element_type` becomes a FalkorDB label by f-string interpolation in
`read_existing_nodes` and the reachability reads, because Cypher cannot parameterize a label. This
is safe because `_validate_element_types` checks every `element_type` against the allow-list
before any query runs (`read_existing_nodes` takes labels only from the built, validated edges),
and the reachability reads iterate this module's own fixed constants.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

import redis.exceptions
from pydantic import ValidationError

from ps_service.dependency_health import FALKORDB, mark_healthy, mark_unhealthy
from ps_service.graph_gateway.models import MutationGroup, NodeRef, UpsertEdge, UpsertNode
from ps_service.ingestion.errors import IngestionPersistenceError
from ps_service.ingestion.models import (
    ReachabilityCount,
    RegulatoryInstrumentMetadata,
    StructuralEdge,
    StructuralNode,
)

if TYPE_CHECKING:
    from ps_service.ingestion.falkordb_client import GraphHandle, GraphQueryResult

_REGULATORY_INSTRUMENT_LABEL = "RegulatoryInstrument"

_KNOWN_ELEMENT_TYPES = frozenset(
    {
        "TITLE",
        "CHAPTER",
        "SECTION",
        "ARTICLE",
        "PARAGRAPH",
        "ANNEX",
        "RECITAL",
    }
)


def _read_query(
    graph: GraphHandle, query: str, params: dict[str, object] | None = None
) -> GraphQueryResult:
    """Run one read query, recording FalkorDB health and wrapping driver errors.

    A `redis.exceptions.RedisError` marks FalkorDB unhealthy (the `/readyz` signal) and is raised
    as `IngestionPersistenceError`; the next successful call marks it healthy again.
    """
    try:
        result = graph.query(query, params=params)
    except redis.exceptions.RedisError as exc:
        mark_unhealthy(FALKORDB, error=exc)
        raise IngestionPersistenceError(f"FalkorDB write failed: {exc}") from exc
    mark_healthy(FALKORDB)
    return result


def _validate_element_types(
    nodes: tuple[StructuralNode, ...],
    edges: tuple[StructuralEdge, ...],
) -> None:
    """Validate every node and edge `element_type` against the allow-list, before anything is built.

    Runs once over the whole collection, not interleaved with building. `"RegulatoryInstrument"`
    is the one allowed parent label outside the allow-list (edges anchoring a top-level node to
    the instrument). Raises `IngestionPersistenceError` on the first violation found.
    """
    for node in nodes:
        if node.element_type not in _KNOWN_ELEMENT_TYPES:
            raise IngestionPersistenceError(
                f"unknown element_type {node.element_type!r} on node {node.id!r}"
            )
    for edge in edges:
        if (
            edge.parent_element_type != _REGULATORY_INSTRUMENT_LABEL
            and edge.parent_element_type not in _KNOWN_ELEMENT_TYPES
        ):
            raise IngestionPersistenceError(
                f"unknown parent_element_type {edge.parent_element_type!r} "
                f"on edge {edge.parent_id!r}->{edge.child_id!r}"
            )
        if edge.child_element_type not in _KNOWN_ELEMENT_TYPES:
            raise IngestionPersistenceError(
                f"unknown child_element_type {edge.child_element_type!r} "
                f"on edge {edge.parent_id!r}->{edge.child_id!r}"
            )


# --- Graph Write Gateway builders ---


@dataclass(frozen=True, slots=True)
class NativeWrites:
    """The primitives of one external ingestion's native graph, validated and not yet submitted.

    Attributes:
        instrument: The RegulatoryInstrument node.
        nodes: The structural nodes, in adapter order.
        edges: Every `HAS` edge in adapter order, including edges whose endpoints exist nowhere;
            `build_native_group` drops those once `read_existing_nodes` has reported what exists.
    """

    instrument: UpsertNode
    nodes: tuple[UpsertNode, ...]
    edges: tuple[UpsertEdge, ...]


type NodeKey = tuple[str, str]
"""A node by `(label, id)`."""

type ExistingNodes = frozenset[NodeKey]
"""The nodes a `read_existing_nodes` read found in the graph."""


def _unstorable(what: str) -> IngestionPersistenceError:
    return IngestionPersistenceError(
        f"{what} has a property the graph cannot store "
        "(a reserved key, or a value that is not finite or is out of range)"
    )


def _instrument_properties(metadata: RegulatoryInstrumentMetadata) -> dict[str, object]:
    """The bibliographic properties of the RegulatoryInstrument node (`effective_date` as ISO)."""
    properties: dict[str, object] = {
        "title": metadata.title,
        "jurisdiction": metadata.jurisdiction,
        "effective_date": metadata.effective_date.isoformat(),
        "version": metadata.version,
        "status": metadata.status,
        "source_type": metadata.source_type,
    }
    if metadata.instrument_type is not None:
        properties["instrument_type"] = metadata.instrument_type
    if metadata.celex is not None:
        properties["celex"] = metadata.celex
    return properties


def _structural_node(node: StructuralNode) -> UpsertNode:
    try:
        return UpsertNode(label=node.element_type, id=node.id, properties=dict(node.properties))
    except ValidationError as exc:
        raise _unstorable(f"{node.element_type} node {node.id!r}") from exc


def _has_edge(regulatory_instrument_id: str, edge: StructuralEdge) -> UpsertEdge:
    """Build one `HAS` edge, replacing the adapter's placeholder RegulatoryInstrument parent id.

    The adapter only knows the CELEX, so every edge anchored to the RegulatoryInstrument carries
    it as `parent_id`; the real node id is the caller-supplied one. Edges between the same two
    nodes share an identity, so they collapse into one.
    """
    parent_id = (
        regulatory_instrument_id
        if edge.parent_element_type == _REGULATORY_INSTRUMENT_LABEL
        else edge.parent_id
    )
    try:
        return UpsertEdge(
            type="HAS",
            identity=f"HAS:{parent_id}->{edge.child_id}",
            source=NodeRef(label=edge.parent_element_type, id=parent_id),
            target=NodeRef(label=edge.child_element_type, id=edge.child_id),
        )
    except ValidationError as exc:
        raise _unstorable(f"HAS edge {parent_id!r} -> {edge.child_id!r}") from exc


def build_native_writes(
    regulatory_instrument_id: str,
    metadata: RegulatoryInstrumentMetadata,
    nodes: tuple[StructuralNode, ...],
    edges: tuple[StructuralEdge, ...],
) -> NativeWrites:
    """Build every primitive of one external ingestion's native graph, or raise before any exist.

    Pure: nothing is read or submitted. The whole collection is validated first, so an invalid
    element anywhere means no primitive reaches the caller.

    Args:
        regulatory_instrument_id: Final `{SHORT}-{VERSION}` id of the RegulatoryInstrument node;
            also substituted for the placeholder parent id on instrument-anchored edges.
        metadata: Bibliographic metadata written as the instrument node's properties.
        nodes: Structural nodes from the adapter; each `element_type` must be in the allow-list
            (TITLE, CHAPTER, SECTION, ARTICLE, PARAGRAPH, ANNEX, RECITAL).
        edges: Parent to child edges from the adapter; element types must be in the allow-list,
            except that a parent may be `RegulatoryInstrument`.

    Returns:
        A `NativeWrites` holding the instrument node, the structural nodes and all `HAS` edges.

    Raises:
        IngestionPersistenceError: an element type is outside the allow-list, or a node carries
            a property the gateway cannot store (a reserved key such as `id`, a non-finite
            number).
    """
    _validate_element_types(nodes, edges)
    try:
        instrument = UpsertNode(
            label=_REGULATORY_INSTRUMENT_LABEL,
            id=regulatory_instrument_id,
            properties=_instrument_properties(metadata),
        )
    except ValidationError as exc:
        raise _unstorable(f"RegulatoryInstrument {regulatory_instrument_id!r}") from exc
    return NativeWrites(
        instrument=instrument,
        nodes=tuple(_structural_node(node) for node in nodes),
        edges=tuple(_has_edge(regulatory_instrument_id, edge) for edge in edges),
    )


def _outside_endpoints(writes: NativeWrites) -> frozenset[NodeKey]:
    """The edge endpoints that are not nodes of this group (they must already be in the graph)."""
    in_group = {(writes.instrument.label, writes.instrument.id)} | {
        (node.label, node.id) for node in writes.nodes
    }
    endpoints = {(ref.label, ref.id) for edge in writes.edges for ref in (edge.source, edge.target)}
    return frozenset(endpoints - in_group)


def read_existing_nodes(graph: GraphHandle, writes: NativeWrites) -> ExistingNodes:
    """Read which of the edge endpoints outside `writes` already exist in the graph.

    One batched read per label, issued once and immediately before the group is submitted; no
    read at all when every endpoint is in the group. The labels come from the validated edges,
    the ids are parameters.

    Args:
        graph: Read handle for the target graph.
        writes: The built primitives whose outside endpoints are looked up.

    Returns:
        The `(label, id)` pairs found; empty when none of the outside endpoints exist or there
        are none.

    Side Effects:
        Reads from FalkorDB and records its health.

    Raises:
        IngestionPersistenceError: FalkorDB could not be reached.
    """
    ids_by_label: dict[str, list[str]] = {}
    for label, node_id in sorted(_outside_endpoints(writes)):
        ids_by_label.setdefault(label, []).append(node_id)
    found: set[NodeKey] = set()
    for label, ids in ids_by_label.items():
        result = _read_query(
            graph, f"MATCH (n:{label}) WHERE n.id IN $ids RETURN n.id", params={"ids": ids}
        )
        rows = cast("list[list[object]]", result.result_set)
        found.update((label, cast("str", row[0])) for row in rows)
    return frozenset(found)


def build_native_group(
    writes: NativeWrites,
    *,
    existing: ExistingNodes,
    graph_name: str,
    audit_event_id: str,
    checkpoint_requested: bool = False,
) -> MutationGroup:
    """Assemble the one native group: the instrument, the structural nodes, then the edges.

    An edge whose endpoint is neither in the group nor in `existing` is dropped silently, because
    the gateway would reject the whole group for it. Dropped edges are never submitted, so
    nothing is logged for them.

    Args:
        writes: The built primitives.
        existing: Nodes already in the graph, from `read_existing_nodes`.
        graph_name: Target graph name.
        audit_event_id: Audit event the group is logged under.
        checkpoint_requested: Asks the gateway to record the graph's digest once the group is
            applied; the pipeline sets it because this group is the last one it writes.

    Returns:
        A `MutationGroup` ordered instrument, structural nodes, surviving edges.
    """
    present = {(writes.instrument.label, writes.instrument.id)} | {
        (node.label, node.id) for node in writes.nodes
    }
    present |= existing
    edges = tuple(
        edge
        for edge in writes.edges
        if (edge.source.label, edge.source.id) in present
        and (edge.target.label, edge.target.id) in present
    )
    return MutationGroup(
        graph=graph_name,
        audit_event_id=audit_event_id,
        primitives=(writes.instrument, *writes.nodes, *edges),
        checkpoint_requested=checkpoint_requested,
    )


def _scalar_count(result: GraphQueryResult) -> int:
    """Return the first cell of a `RETURN count(...)` result as an int; no rows counts as zero.

    The casts recover the row shape and the `int` cell that `GraphQueryResult.result_set`
    (typed `list[object]`) does not express.
    """
    rows = cast("list[list[object]]", result.result_set)
    if not rows:
        return 0  # a read-only query of a graph key that does not exist answers with no rows
    return cast("int", rows[0][0])


def _count_nodes(graph: GraphHandle, label: str) -> int:
    return _scalar_count(_read_query(graph, f"MATCH (n:{label}) RETURN count(n)"))


def _count_orphans(graph: GraphHandle, label: str) -> int:
    """Count nodes of `label` not reachable from any RegulatoryInstrument via `HAS` (any depth).

    Version-agnostic: one `{short}_native` graph can hold several versions' subtrees, each
    anchored to its own `{SHORT}-{VERSION}` RegulatoryInstrument node (a new version supersedes,
    never overwrites). A node owned by a sibling version is reachable from some instrument, so it
    is not an orphan; only a node left unlinked from every instrument is counted.
    """
    return _scalar_count(
        _read_query(
            graph,
            f"MATCH (n:{label}) WHERE NOT (:RegulatoryInstrument)-[:HAS*1..]->(n) RETURN count(n)",
        )
    )


def verify_structural_graph_reachable(
    graph: GraphHandle, regulatory_instrument_id: str
) -> dict[str, ReachabilityCount]:
    """Check that every structural node is reachable from some RegulatoryInstrument.

    For every structural label plus `RegulatoryInstrument`, counts the whole-graph nodes
    (`total`) and how many are linked into a RegulatoryInstrument's `HAS` tree
    (`reachable = total - orphans`). The `RegulatoryInstrument` entry always has
    `reachable == total`. On a clean graph, single- or multi-version, `reachable == total` for
    every label, because only nodes unreachable from every instrument count as orphans.

    Args:
        graph: Read handle for the native graph.
        regulatory_instrument_id: Id of the instrument just written; used only in the error
            message.

    Returns:
        A `ReachabilityCount` per label.

    Side Effects:
        Reads from FalkorDB and records its health.

    Raises:
        IngestionPersistenceError: any label has an orphan (the write left a node unlinked), or
            FalkorDB could not be reached.
    """
    counts: dict[str, ReachabilityCount] = {}
    for label in (_REGULATORY_INSTRUMENT_LABEL, *_KNOWN_ELEMENT_TYPES):
        total = _count_nodes(graph, label)
        if label == _REGULATORY_INSTRUMENT_LABEL:
            counts[label] = ReachabilityCount(total=total, reachable=total)
            continue
        orphans = _count_orphans(graph, label)
        if orphans != 0:
            raise IngestionPersistenceError(
                f"{label}: {orphans} of {total} node(s) not reachable from any "
                f"RegulatoryInstrument (persist left an orphan) — re-ingest of "
                f"{regulatory_instrument_id!r}"
            )
        counts[label] = ReachabilityCount(total=total, reachable=total - orphans)
    return counts
