"""FalkorDB persistence for `ps_service.ingestion`'s RegulatoryInstrument node and
native structural graph.

Implements PLAN_REVIEWED.md §7 Increments 8-10:

- `register_regulation_version` — parameterized MERGE of the RegulatoryInstrument
  node. `"RegulatoryInstrument"` is a fixed-literal label (never externally sourced),
  so no allow-list check applies to it — contrast `_upsert_node` below,
  whose label comes from adapter-supplied `element_type` and must be
  allow-listed before every write.
- `persist_native_structural_graph` — THE B1 FIX. `_validate_element_types`
  validates every node's/edge's `element_type` against
  `_KNOWN_ELEMENT_TYPES` in a whole-collection first pass, raising
  `IngestionPersistenceError` on the first violation found, before
  `persist_native_structural_graph` issues a single `graph.query()` call
  for any element (PLAN_REVIEWED.md §4.2). Only after validation passes
  completely does this function loop and write every node/edge — this is
  what makes the CA doc's `PersistNativeStructuralGraph` post-condition
  ("Abort with no partial write if structure can't be fully persisted")
  actually hold for a real 200-800+ node regulation, instead of the
  original per-element-interleaved design that let earlier nodes get
  written before a later invalid element was ever seen.
- `verify_structural_graph_reachable` — AC-004's reachability check: counts
  every structural label's total vs. how many are actually reachable from
  the RegulatoryInstrument node via `HAS` (any depth). Raises
  `IngestionPersistenceError` — not a silent warning — on any gap.

Structural node/edge ids are used exactly as produced by the Cellar/ELI
adapter (CELEX-prefixed, e.g. `"32024R2847#art_1"`) — never rewritten or
re-derived here. This is an explicit orchestrator decision (see
`.orchestrator/tracker/issue-14-ingestion-adapter/CONTEXT.md`'s "Structural
node id prefixing" section): graph-per-regulation scoping (one FalkorDB
graph per regulation, `falkordb_client.native_graph_name`, PLAN_REVIEWED.md
§4.1) already makes CELEX-prefixed ids globally unique within their graph
and deterministic across re-ingest, so AC-004's reachability/count checks
below are traversal- and count-based, not id-string-based.

**Increment 13 fix — RegulatoryInstrument-anchored edge endpoint substitution.** The
adapter never sees the final `{SHORT}-{VERSION}` regulation id (only the
raw CELEX `identifier` it was called with — see the id-prefixing decision
above), so every `StructuralEdge` it produces with `parent_element_type ==
"RegulatoryInstrument"` carries that CELEX identifier as a *placeholder*
`parent_id`, not the real RegulatoryInstrument node's id. The real RegulatoryInstrument node is
registered separately by `register_regulation_version`, keyed by the
caller-supplied `regulation_id` (`pipeline.py`'s `f"{short_name}-{version}"`).
Left unsubstituted, `_upsert_edge`'s `MATCH (a:RegulatoryInstrument {id: $parent_id})`
matches zero rows for every top-level structural edge (the CELEX string
never equals any real RegulatoryInstrument node's id), so the `MERGE` silently never
fires — every Chapter/Recital/Annex that anchors directly to RegulatoryInstrument
would end up completely unreachable, breaking AC-004 for the entire
graph, not just those top-level nodes (anything nested under them becomes
unreachable transitively too). `persist_native_structural_graph`'s
`regulation_id` parameter — previously unused, see IMPL_8_10.md's flagged
deviation #1 — is exactly what closes this gap: `_upsert_edge` now
substitutes it for `edge.parent_id` whenever `edge.parent_element_type ==
"RegulatoryInstrument"`, and uses `edge.parent_id` unchanged for every other edge.
This is not a rewrite of the structural id space (`StructuralNode.id`/
non-RegulatoryInstrument `StructuralEdge` endpoints are untouched) — only the one
endpoint the adapter cannot know by design. Found and fixed via
Increment 13's live 3-regulation run (CRA's ANNEX nodes: 8 persisted, 0
reachable, before this fix) — the existing fake-graph unit tests below
never caught this because their own `_edge(...)` fixtures already set
`parent_id` equal to `regulation_id` by construction, never modeling the
real CELEX-vs-final-id mismatch the live adapter->pipeline handoff
produces.

Query Safety (L2): `element_type` becomes a FalkorDB node/edge-endpoint
label via f-string interpolation in `_upsert_node`/`_upsert_edge` — Cypher
cannot parameterize a label. This is safe *only* because
`_validate_element_types` already checked every element_type against the
allow-list before any query executes; the allow-list check IS the safety
boundary, not anything inside the f-string itself.
`_upsert_node`/`_upsert_edge` never re-check — they only ever issue the
write. `verify_structural_graph_reachable` interpolates a label the same
way, but its labels come from iterating `_KNOWN_ELEMENT_TYPES` /
`_REGULATION_LABEL` directly (this module's own fixed constants), not from
any externally-sourced value, so no separate allow-list check is needed
there either.
"""

from __future__ import annotations

from typing import cast

import redis.exceptions

from ps_service.dependency_health import FALKORDB, mark_healthy, mark_unhealthy
from ps_service.ingestion.errors import IngestionPersistenceError
from ps_service.ingestion.falkordb_client import GraphHandle, GraphQueryResult
from ps_service.ingestion.models import (
    ReachabilityCount,
    RegulationMetadata,
    StructuralEdge,
    StructuralNode,
)

_REGULATION_LABEL = "RegulatoryInstrument"

_KNOWN_ELEMENT_TYPES = frozenset({
    "TITLE",
    "CHAPTER",
    "SECTION",
    "ARTICLE",
    "PARAGRAPH",
    "ANNEX",
    "RECITAL",
})


def _execute_query(
    graph: GraphHandle, query: str, params: dict[str, object] | None = None
) -> GraphQueryResult:
    """The one call site every `graph.query()` write/read in this module goes
    through, so FalkorDB connectivity failures get recorded in
    `ps_service.dependency_health` for `/ready`'s live signal, self-healing
    on the next successful call the same way `falkordb_client.check_connectivity`
    already does for the startup probe.

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


def register_regulation_version(
    graph: GraphHandle, regulation_id: str, metadata: RegulationMetadata
) -> None:
    """MERGE the RegulatoryInstrument node, keyed by `regulation_id`
    (`{SHORT}-{VERSION}`, computed by the caller — see `pipeline.py`, never
    by this function or an adapter).

    All bibliographic fields flow through `params` only, never
    interpolated into the query string (L2 Query Safety's parameterization
    rule) — the query string itself contains no metadata value, only
    `RegulatoryInstrument` (a fixed literal, not user/adapter-sourced) and the `$id`/
    `$properties` placeholder names. `effective_date` is serialized via
    `.isoformat()` since FalkorDB query params don't accept a raw
    `datetime.date` object.
    """
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
    _execute_query(
        graph,
        "MERGE (n:RegulatoryInstrument {id: $id}) SET n += $properties",
        params={"id": regulation_id, "properties": properties},
    )


def _validate_element_types(
    nodes: tuple[StructuralNode, ...],
    edges: tuple[StructuralEdge, ...],
) -> None:
    """B1 FIX: a whole-collection validation pass. Called once, before
    `persist_native_structural_graph` issues a single `graph.query` call
    for ANY element — not interleaved validate-then-write per element.
    Checks every node's `element_type` and every edge's parent/child
    `element_type` (`"RegulatoryInstrument"` is the one allowed non-`element_type`
    parent label, for edges anchoring a top-level structural node directly
    to the RegulatoryInstrument node) against the allow-list. Raises
    `IngestionPersistenceError` on the first violation found, with zero
    `graph.query` calls having been made by the time this raises.
    """
    for node in nodes:
        if node.element_type not in _KNOWN_ELEMENT_TYPES:
            raise IngestionPersistenceError(
                f"unknown element_type {node.element_type!r} on node {node.id!r}"
            )
    for edge in edges:
        if (
            edge.parent_element_type != _REGULATION_LABEL
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


def persist_native_structural_graph(
    graph: GraphHandle,
    regulation_id: str,
    nodes: tuple[StructuralNode, ...],
    edges: tuple[StructuralEdge, ...],
) -> None:
    """CA doc post-condition: "Abort with no partial write if structure
    can't be fully persisted." Validation of the whole collection
    (`_validate_element_types`) happens entirely before any write (B1 fix)
    — if this function raises, the graph has recorded zero `query()` calls.
    Only once validation passes completely does this loop and call
    `_upsert_node`/`_upsert_edge` for every element.

    `regulation_id` is threaded into `_upsert_edge` (Increment 13 fix, see
    module docstring): every edge whose `parent_element_type == "RegulatoryInstrument"`
    has its `parent_id` substituted with this real, caller-supplied id at
    write time, since the adapter that produced `edges` could only ever
    stamp a CELEX-based placeholder there.
    """
    _validate_element_types(nodes, edges)
    for node in nodes:
        _upsert_node(graph, node)
    for edge in edges:
        _upsert_edge(graph, regulation_id, edge)


def _upsert_node(graph: GraphHandle, node: StructuralNode) -> None:
    # element_type already validated by _validate_element_types above —
    # this function only ever issues the write, never the check.
    _execute_query(
        graph,
        f"MERGE (n:{node.element_type} {{id: $id}}) SET n += $properties",
        params={"id": node.id, "properties": node.properties},
    )


def _upsert_edge(graph: GraphHandle, regulation_id: str, edge: StructuralEdge) -> None:
    # parent/child element_type already validated by _validate_element_types
    # above — this function only ever issues the write, never the check.
    #
    # Increment 13 fix: `edge.parent_id` is a CELEX-based placeholder
    # whenever `edge.parent_element_type == "RegulatoryInstrument"` (the adapter
    # cannot know the real, caller-supplied `regulation_id` — see this
    # module's docstring). Substitute the real id for that one case only;
    # every other edge's `parent_id` (a StructuralNode's own id) is used
    # unchanged.
    parent_id = regulation_id if edge.parent_element_type == _REGULATION_LABEL else edge.parent_id
    _execute_query(
        graph,
        f"MATCH (a:{edge.parent_element_type} {{id: $parent_id}}), "
        f"(b:{edge.child_element_type} {{id: $child_id}}) "
        "MERGE (a)-[:HAS]->(b)",
        params={"parent_id": parent_id, "child_id": edge.child_id},
    )


def _scalar_count(result: GraphQueryResult) -> int:
    """Extract a single integer from a `RETURN count(...)`/
    `RETURN count(DISTINCT ...)` query's first row, first column.

    `cast` used twice, unavoidably: `GraphQueryResult.result_set` is typed
    `list[object]` (matching `falkordb.QueryResult`'s own untyped shape —
    the library ships no more specific stub), so a first cast recovers the
    row-of-columns shape a real FalkorDB result actually has, and a second
    recovers the cell's runtime `int` value from a `count(...)` clause.
    """
    rows = cast("list[list[object]]", result.result_set)
    return cast(int, rows[0][0])


def _count_nodes(graph: GraphHandle, label: str) -> int:
    return _scalar_count(_execute_query(graph, f"MATCH (n:{label}) RETURN count(n)"))


def _count_reachable(graph: GraphHandle, regulation_id: str, label: str) -> int:
    return _scalar_count(
        _execute_query(
            graph,
            f"MATCH (:RegulatoryInstrument {{id: $id}})-[:HAS*1..]->(n:{label}) RETURN count(DISTINCT n)",
            params={"id": regulation_id},
        )
    )


def verify_structural_graph_reachable(
    graph: GraphHandle, regulation_id: str
) -> dict[str, ReachabilityCount]:
    """AC-004: for every structural label (plus `RegulatoryInstrument` itself), the
    node count in this regulation's own graph versus how many of those
    nodes are actually reachable from the RegulatoryInstrument node via `HAS` (any
    depth) — not just present. One FalkorDB graph per regulation
    (`falkordb_client.native_graph_name`, PLAN_REVIEWED.md §4.1), so a bare
    `MATCH (n:LABEL)` total is already this regulation's own total; no
    id-prefix scoping is needed (§4.3).

    Raises `IngestionPersistenceError` — not a silent warning — if any
    label has `reachable != total`, matching the CA doc's
    `PersistNativeStructuralGraph` post-condition (no partial/undetected
    write left behind).
    """
    counts: dict[str, ReachabilityCount] = {}
    for label in (_REGULATION_LABEL, *_KNOWN_ELEMENT_TYPES):
        total = _count_nodes(graph, label)
        reachable = (
            total if label == _REGULATION_LABEL else _count_reachable(graph, regulation_id, label)
        )
        counts[label] = ReachabilityCount(total=total, reachable=reachable)
        if reachable != total:
            raise IngestionPersistenceError(
                f"{label}: total={total} but only {reachable} reachable "
                f"from RegulatoryInstrument {regulation_id!r}"
            )
    return counts
