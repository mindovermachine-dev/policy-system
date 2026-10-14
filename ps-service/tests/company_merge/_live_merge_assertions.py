"""Shared snapshot/assertion helpers for `company_merge`'s live-graph tests.

Used by the live capstone and by the real-`policy_system` verification: both need identical
assertion logic, not re-typed copies that could drift apart. The every-node-type and
traversal assertions take a keyword-only `graph_name` so a failing assertion names the graph
actually checked.

Not collected by pytest: the default `python_files` pattern (`test_*.py`, this repo's root
`pyproject.toml`) never matches this filename. The leading underscore is a convention mirroring
the package's `_fakes.py`: a shared support module, not a test module.

`_snapshot_node_properties`/`_snapshot_edge_properties`/`_snapshot_edge_ids` take property-level
and edge-existence snapshots, scoped to what `ps_service/company_merge/graph_writer.py` writes:

- `persist_rewired_edges` writes `HAS`/`SATISFIED_BY`/`REQUIRES` via a bare
  `MATCH ... MERGE (s)-[:TYPE]->(t)` with no `SET` clause, so these three relationship types
  never carry an edge property. Existence-survival only.
- `_upsert_provenance_edge`, used for `DEFINES`/`EXPRESSES`, always issues
  `SET e.source_ref = $source_ref`, so these two DO carry properties and get a real
  property-level snapshot.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from ps_service.company_merge.falkordb_client import GraphHandle

_NODE_LABELS = ("RegulatoryInstrument", "Role", "Requirement", "Obligation", "Capability")
_EDGE_TYPES = ("DEFINES", "EXPRESSES", "HAS", "SATISFIED_BY", "REQUIRES")

# Split confirmed against graph_writer.py (module docstring above): only these two
# relationship types ever carry an edge property.
_PROPERTY_BEARING_EDGE_TYPES = ("DEFINES", "EXPRESSES")
# ... and these three never do -- existence-survival only.
_PROPERTYLESS_EDGE_TYPES = ("HAS", "SATISFIED_BY", "REQUIRES")


def _query_rows(
    graph: GraphHandle, query: str, params: dict[str, object] | None = None
) -> list[list[object]]:
    return cast("list[list[object]]", graph.query(query, params=params).result_set)


def _count(graph: GraphHandle, query: str, params: dict[str, object] | None = None) -> int:
    return cast("int", _query_rows(graph, query, params=params)[0][0])


def _snapshot_counts(single_tenant_graph: GraphHandle) -> dict[str, int]:  # pyright: ignore[reportUnusedFunction]
    """Per-label node counts + per-relationship-type edge counts -- the
    "no growth" proof operates on this whole snapshot, not just a single
    total.
    """
    counts = {
        label: _count(single_tenant_graph, f"MATCH (n:{label}) RETURN count(n)")
        for label in _NODE_LABELS
    }
    counts.update(
        {
            rel: _count(single_tenant_graph, f"MATCH ()-[r:{rel}]->() RETURN count(r)")
            for rel in _EDGE_TYPES
        }
    )
    return counts


def _snapshot_embeddings(single_tenant_graph: GraphHandle) -> dict[str, tuple[float, ...] | None]:  # pyright: ignore[reportUnusedFunction]
    """Id -> embedding (or None) for every Obligation/Capability node --
    the "zero further embedding-backfill writes" proof compares this
    whole map before/after the second pass, not just the seeded pair.
    """
    embeddings: dict[str, tuple[float, ...] | None] = {}
    for label in ("Obligation", "Capability"):
        for node_id, embedding in _query_rows(
            single_tenant_graph, f"MATCH (n:{label}) RETURN n.id, n.embedding"
        ):
            embeddings[cast("str", node_id)] = (
                tuple(cast("list[float]", embedding)) if embedding is not None else None
            )
    return embeddings


def _assert_ac001_every_node_type_present(  # pyright: ignore[reportUnusedFunction]
    single_tenant_graph: GraphHandle, regulatory_instrument_id: str, *, graph_name: str
) -> None:
    assert (
        _count(
            single_tenant_graph,
            "MATCH (n:RegulatoryInstrument {id: $id}) RETURN count(n)",
            {"id": regulatory_instrument_id},
        )
        == 1
    ), f"{regulatory_instrument_id}: Regulation node missing from {graph_name}"

    role_count = _count(
        single_tenant_graph,
        "MATCH (:RegulatoryInstrument {id: $id})-[:DEFINES]->(:Role) RETURN count(*)",
        {"id": regulatory_instrument_id},
    )
    requirement_count = _count(
        single_tenant_graph,
        "MATCH (:RegulatoryInstrument {id: $id})-[:EXPRESSES]->(:Requirement) RETURN count(*)",
        {"id": regulatory_instrument_id},
    )
    obligation_count = _count(
        single_tenant_graph,
        "MATCH (:RegulatoryInstrument {id: $id})-[:DEFINES]->(:Role)-[:HAS]->(:Obligation) "
        "RETURN count(*)",
        {"id": regulatory_instrument_id},
    )
    capability_count = _count(
        single_tenant_graph,
        "MATCH (:RegulatoryInstrument {id: $id})-[:DEFINES]->(:Role)-[:HAS]->(:Obligation)"
        "-[:REQUIRES]->(:Capability) "
        "RETURN count(*)",
        {"id": regulatory_instrument_id},
    )
    assert role_count > 0, f"{regulatory_instrument_id}: no Role reachable via DEFINES"
    assert requirement_count > 0, (
        f"{regulatory_instrument_id}: no Requirement reachable via EXPRESSES"
    )
    assert obligation_count > 0, f"{regulatory_instrument_id}: no Obligation reachable via Role HAS"
    assert capability_count > 0, (
        f"{regulatory_instrument_id}: no Capability reachable via Obligation REQUIRES"
    )


def _assert_ac006_traversal_reachable(  # pyright: ignore[reportUnusedFunction]
    single_tenant_graph: GraphHandle, regulatory_instrument_id: str, *, graph_name: str
) -> None:
    has_chain_count = _count(
        single_tenant_graph,
        "MATCH (:RegulatoryInstrument {id: $id})-[:DEFINES]->(:Role)-[:HAS]->(:Obligation)"
        "-[:REQUIRES]->(:Capability) "
        "RETURN count(*)",
        {"id": regulatory_instrument_id},
    )
    satisfied_chain_count = _count(
        single_tenant_graph,
        "MATCH (:RegulatoryInstrument {id: $id})-[:EXPRESSES]->(:Requirement)"
        "-[:SATISFIED_BY]->(:Obligation) "
        "RETURN count(*)",
        {"id": regulatory_instrument_id},
    )
    assert has_chain_count > 0, (
        f"{regulatory_instrument_id}: no live "
        f"Regulation->DEFINES->Role->HAS->Obligation->REQUIRES->"
        f"Capability traversal in {graph_name}"
    )
    assert satisfied_chain_count > 0, (
        f"{regulatory_instrument_id}: no live "
        f"Regulation->EXPRESSES->Requirement->SATISFIED_BY->Obligation "
        f"traversal in {graph_name}"
    )


def _assert_run_id_logged(  # pyright: ignore[reportUnusedFunction]
    log_entries: list[dict[str, object]], *, action: str, run_id: str, entity_id: str
) -> None:
    matches = [
        entry
        for entry in log_entries
        if entry.get("action") == action and entry.get("run_id") == run_id
    ]
    assert matches, f"no log entry found for action={action!r} run_id={run_id!r}"
    assert any(
        entry.get("entity_id") == entity_id and entry.get("outcome") == "succeeded"
        for entry in matches
    ), f"no succeeded entry with entity_id={entity_id!r} for action={action!r} run_id={run_id!r}"


def _assert_dedup_decisions_correlated(log_entries: list[dict[str, object]], run_id: str) -> None:  # pyright: ignore[reportUnusedFunction]
    matches = [
        entry
        for entry in log_entries
        if entry.get("action") == "dedupe_canonical_nodes" and entry.get("run_id") == run_id
    ]
    assert matches, (
        f"no dedupe_canonical_nodes log entries correlated to run_id={run_id!r} (AC-007)"
    )


def _assert_ac008_no_role_or_requirement_dedup(log_entries: list[dict[str, object]]) -> None:  # pyright: ignore[reportUnusedFunction]
    dedup_entries = [
        entry for entry in log_entries if entry.get("action") == "dedupe_canonical_nodes"
    ]
    assert dedup_entries, (
        "expected at least one dedupe_canonical_nodes log entry across the whole run"
    )
    for entry in dedup_entries:
        entity_id = cast("str", entry.get("entity_id"))
        assert entity_id.startswith("cap_"), (
            f"dedupe_canonical_nodes fired for entity_id={entity_id!r}, which is not a "
            "Capability (cap_*) id -- since #42, Role/Requirement/Obligation dedup is all "
            "out of scope (AC-008); only Capability is deduped"
        )


def _snapshot_node_properties(  # pyright: ignore[reportUnusedFunction]
    single_tenant_graph: GraphHandle,
) -> dict[str, dict[str, dict[str, object]]]:
    """Per-label `{id: properties}` for every node label the
    before/after property-level integrity proof needs:
    `RegulatoryInstrument`/`Role`/`Requirement`/`Obligation`/`Capability`.

    The `embedding` key is stripped at CAPTURE time (not comparison time)
    for `Capability` -- a semantic match's embedding can legitimately be
    backfilled between two snapshots (the existing backfill
    mechanism) without that being a genuine before/after integrity
    violation for this proof's purposes.
    """
    snapshot: dict[str, dict[str, dict[str, object]]] = {}
    for label in _NODE_LABELS:
        by_id: dict[str, dict[str, object]] = {}
        for node_id, props in _query_rows(
            single_tenant_graph, f"MATCH (n:{label}) RETURN n.id AS id, properties(n) AS props"
        ):
            properties = dict(cast("dict[str, object]", props))
            if label == "Capability":
                properties.pop("embedding", None)
            by_id[cast("str", node_id)] = properties
        snapshot[label] = by_id
    return snapshot


def _snapshot_edge_properties(  # pyright: ignore[reportUnusedFunction]
    single_tenant_graph: GraphHandle,
) -> dict[str, dict[str, dict[str, object]]]:
    """Per-relationship-type `{"source_id|target_id": properties}` for the
    only two relationship types that carry any --
    `DEFINES`/`EXPRESSES`, confirmed against `graph_writer._upsert_provenance_edge`'s
    `SET e.source_ref = $source_ref`. `HAS`/`SATISFIED_BY`/`REQUIRES` carry
    none at all -- see `_snapshot_edge_ids`.
    """
    snapshot: dict[str, dict[str, dict[str, object]]] = {}
    for relationship_type in _PROPERTY_BEARING_EDGE_TYPES:
        by_endpoint_pair: dict[str, dict[str, object]] = {}
        for source_id, target_id, props in _query_rows(
            single_tenant_graph,
            f"MATCH (a)-[e:{relationship_type}]->(b) RETURN a.id, b.id, properties(e)",
        ):
            key = f"{cast('str', source_id)}|{cast('str', target_id)}"
            by_endpoint_pair[key] = dict(cast("dict[str, object]", props))
        snapshot[relationship_type] = by_endpoint_pair
    return snapshot


def _snapshot_edge_ids(single_tenant_graph: GraphHandle) -> dict[str, list[list[str]]]:  # pyright: ignore[reportUnusedFunction]
    """Per-relationship-type `(source_id, target_id)` existence pairs for the
    three relationship types that carry NO properties --
    `HAS`/`SATISFIED_BY`/`REQUIRES`, confirmed against
    `graph_writer.persist_rewired_edges`'s bare `MERGE (s)-[:TYPE]->(t)`
    (no `SET` clause at all). Existence-survival only: the
    before/after proof for these three checks `before[rel_type]` is a subset
    of `after[rel_type]`, never a property-level comparison, since there is
    no property to compare.
    """
    snapshot: dict[str, list[list[str]]] = {}
    for relationship_type in _PROPERTYLESS_EDGE_TYPES:
        pairs: list[list[str]] = []
        for source_id, target_id in _query_rows(
            single_tenant_graph, f"MATCH (a)-[:{relationship_type}]->(b) RETURN a.id, b.id"
        ):
            pairs.append([cast("str", source_id), cast("str", target_id)])
        snapshot[relationship_type] = pairs
    return snapshot
