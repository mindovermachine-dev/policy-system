"""The native structural graph of one external ingestion: what is written, and how it is checked.

Since issue #208 nothing here writes to FalkorDB. The module builds the primitives the Graph Write
Gateway applies (`pipeline.py` submits them) and reads the graph to check the result:

- `build_native_writes` — validates and builds the RegulatoryInstrument node, the structural nodes
  and the `HAS` edges. `_validate_element_types` is THE B1 FIX: it validates every node's/edge's
  `element_type` against `_KNOWN_ELEMENT_TYPES` in a whole-collection first pass, raising
  `IngestionPersistenceError` on the first violation found, before a single primitive exists
  (PLAN_REVIEWED.md §4.2), so the CA doc's `PersistNativeStructuralGraph` post-condition
  ("Abort with no partial write if structure can't be fully persisted") holds for a real
  200-800+ node regulation: the whole graph is one group, which the gateway commits or rejects
  as a whole.
- `read_existing_nodes` / `build_native_group` — an edge whose endpoint is neither in the group
  nor already in the graph is dropped, silently (AC-BI-004). The legacy `MATCH ... MERGE` matched
  zero rows for it; the gateway would reject the whole group. One batched read per label, issued
  once, immediately before the submit (a node deleted in between makes the gateway reject the
  group: the pre-flight/write race that was always accepted, mapped to the persistence error).
- `verify_structural_graph_reachable` — AC-004's reachability check: for every structural label,
  no node may be an orphan — unreachable from *every* RegulatoryInstrument node via `HAS` (any
  depth). Version-agnostic (a `{short}_native` graph can hold several coexisting versions'
  subtrees after a Change-Monitor re-ingest). Raises `IngestionPersistenceError` — not a silent
  warning — on any orphan. It reads the live graph, so the caller runs it only once the graph
  holds everything committed (`graph_submission.require_caught_up`).

Structural node/edge ids are used exactly as produced by the Cellar/ELI
adapter (CELEX-prefixed, e.g. `"32024R2847#art_1"`) — never rewritten or
re-derived here. This is an explicit orchestrator decision (see
`.orchestrator/tracker/issue-14-ingestion-adapter/CONTEXT.md`'s "Structural
node id prefixing" section): graph-per-regulation scoping (one FalkorDB
graph per regulation, `falkordb_client.native_graph_name`, PLAN_REVIEWED.md
§4.1) already makes CELEX-prefixed ids globally unique within their graph
and deterministic across re-ingest, so AC-004's reachability/count checks
are traversal- and count-based, not id-string-based.

**Increment 13 fix — RegulatoryInstrument-anchored edge endpoint substitution.** The
adapter never sees the final `{SHORT}-{VERSION}` regulation id (only the
raw CELEX `identifier` it was called with — see the id-prefixing decision
above), so every `StructuralEdge` it produces with `parent_element_type ==
"RegulatoryInstrument"` carries that CELEX identifier as a *placeholder*
`parent_id`, not the real RegulatoryInstrument node's id. `_has_edge` substitutes the
caller-supplied `regulatory_instrument_id` for it, and uses `edge.parent_id` unchanged for every
other edge. Left unsubstituted, every top-level structural edge would name a node that does not
exist, and every Chapter/Recital/Annex that anchors directly to the RegulatoryInstrument (and
everything nested under them) would end up unreachable, breaking AC-004 for the whole graph.

Gateway delta (issue #208): every edge carries an `identity`, `HAS:<parent>-><child>` (the gateway's
merge key; the legacy edges had none), and a node property the gateway reserves (`id`,
`embedding`) or cannot store (a non-finite number) is rejected instead of silently rewriting the
node id.

Query Safety (L2): `element_type` becomes a FalkorDB label via f-string interpolation in
`read_existing_nodes` and the reachability reads — Cypher cannot parameterize a label. This is
safe because `_validate_element_types` already checked every element_type against the allow-list
before any query executes (`read_existing_nodes` only takes labels from the built, validated
edges); the reachability reads iterate this module's own fixed constants.
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
    """The one call site every `graph.query()` read in this module goes through.

    FalkorDB connectivity failures get recorded in
    `ps_service.dependency_health` for `/ready`'s live signal, self-healing
    on the next successful call the same way
    `falkordb_client.check_connectivity` already does for the startup
    probe.

    Wraps `redis.exceptions.RedisError` — the base class every
    connection/timeout error the underlying `falkordb`/`redis-py` stack
    raises subclasses — into `IngestionPersistenceError`, distinct from this
    module's own data-validation `IngestionPersistenceError`s
    (`_validate_element_types`, `verify_structural_graph_reachable`), which
    are raised directly by this module's own logic and never reach here.
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
    """B1 FIX: a whole-collection validation pass.

    Called once, before `build_native_writes` builds a single primitive for ANY element — not
    interleaved validate-then-build per element. Checks every node's `element_type` and
    every edge's parent/child `element_type` (`"RegulatoryInstrument"` is
    the one allowed non-`element_type` parent label, for edges anchoring a
    top-level structural node directly to the RegulatoryInstrument node)
    against the allow-list. Raises `IngestionPersistenceError` on the first
    violation found, with nothing built by the time this raises.
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


# --- Graph Write Gateway builders (issue #208) ---


@dataclass(frozen=True, slots=True)
class NativeWrites:
    """The primitives of one external ingestion's native graph, validated and not yet submitted.

    `instrument` is the RegulatoryInstrument node, `nodes` the structural nodes in adapter
    order, `edges` every `HAS` edge in adapter order (before the endpoints that exist nowhere
    are dropped, which needs a read of the graph: `read_existing_nodes`).
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
    """One `HAS` edge; the adapter's placeholder RegulatoryInstrument parent id is replaced.

    The adapter only knows the CELEX, so every edge anchored to the RegulatoryInstrument carries
    it as `parent_id` (Increment 13); the real node id is the caller-supplied one. Same-type
    edges between the same two nodes share an identity, as `MERGE (a)-[:HAS]->(b)` collapsed them.
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

    Pure: nothing is read or submitted. The whole collection is validated first
    (`_validate_element_types`, the B1 fix), so an invalid element anywhere means no primitive
    reaches the caller.

    Raises:
        IngestionPersistenceError: an element type is outside the allow-list, or a node carries
            a property the gateway cannot store (a reserved key such as `id`, a non-finite
            number); the latter was silently accepted by the legacy `SET n += $properties`.
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
    read at all when every endpoint is in the group. A read, so the write handle stays out of it.
    The labels come from the validated edges, the ids are parameters (Query Safety).

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
) -> MutationGroup:
    """Assemble the one native group: the instrument, the structural nodes, then the edges.

    An edge whose endpoint is neither in the group nor in `existing` is dropped, silently
    (AC-BI-004): `MATCH ... MERGE` matched zero rows for it before, and the gateway would reject
    the whole group for it. Dropped edges are never submitted, so nothing is logged for them.
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
    )


def _scalar_count(result: GraphQueryResult) -> int:
    """Extract a single integer from a `RETURN count(...)` query's first cell.

    Reads the first row, first column of a `RETURN count(...)` /
    `RETURN count(DISTINCT ...)` result.
    `cast` used twice, unavoidably: `GraphQueryResult.result_set` is typed
    `list[object]` (matching `falkordb.QueryResult`'s own untyped shape —
    the library ships no more specific stub), so a first cast recovers the
    row-of-columns shape a real FalkorDB result actually has, and a second
    recovers the cell's runtime `int` value from a `count(...)` clause.
    """
    rows = cast("list[list[object]]", result.result_set)
    return cast("int", rows[0][0])


def _count_nodes(graph: GraphHandle, label: str) -> int:
    return _scalar_count(_read_query(graph, f"MATCH (n:{label}) RETURN count(n)"))


def _count_orphans(graph: GraphHandle, label: str) -> int:
    """Nodes of `label` not reachable from ANY RegulatoryInstrument via `HAS` (any depth).

    Version-agnostic denominator for the reachability check: after a
    Change-Monitor re-ingest, one `{short}_native` graph holds several
    versions' structural subtrees, each anchored to its own
    `{SHORT}-{VERSION}` RegulatoryInstrument node (domain-concepts: a new
    version supersedes, never overwrites). A node owned by a sibling version
    is still reachable from *some* RegulatoryInstrument, so it is not an
    orphan; only a node that a `persist` wrote but left unlinked from every
    RegulatoryInstrument is counted.
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
    """AC-004: every structural node must be reachable from some RegulatoryInstrument.

    For every structural label (plus `RegulatoryInstrument` itself), reports
    the whole-graph node count (`total`) and how many of those are actually
    linked into a RegulatoryInstrument's `HAS` tree
    (`reachable = total - orphans`). `total` stays a whole-graph count so the
    public shape is unchanged; `reachable` equals `total` on every clean
    graph, single- or multi-version, because the orphan denominator counts
    only nodes unreachable from *every* RegulatoryInstrument.

    Raises `IngestionPersistenceError` — not a silent warning — if any label
    has an orphan, matching the CA doc's `PersistNativeStructuralGraph`
    post-condition (no partial/undetected write left behind). A node this run
    wrote but failed to link is unreachable from every RegulatoryInstrument
    (its id prefix is unique to this run), so it is always caught.
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
