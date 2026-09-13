"""Shared snapshot/assertion helpers for `company_merge`'s live-graph tests.

Issue #28 (CHANGES.md #6): `_snapshot_counts`/`_snapshot_embeddings`/
`_assert_ac001_every_node_type_present`/`_assert_ac006_traversal_reachable`/
`_assert_run_id_logged`/`_assert_dedup_decisions_correlated`/
`_assert_ac008_no_role_or_requirement_dedup` are extracted VERBATIM out of
`test_live_capstone.py` -- a pure, behavior-preserving refactor. DRY (L1):
this issue's own real-`policy_system` verification needs the *identical*
assertion logic #16's own capstone already proved out, not a re-typed copy
that could silently drift from that proof's shape. `test_live_capstone.py`
re-imports these from here; its own test behavior/output is unchanged.

`_assert_ac001_every_node_type_present`/`_assert_ac006_traversal_reachable`
gained a new required `graph_name` keyword-only parameter here, replacing
`test_live_capstone.py`'s own hardcoded `_CAPSTONE_GRAPH_NAME` module
constant inside their failure-message f-strings -- every call site in
`test_live_capstone.py` now passes `graph_name=_CAPSTONE_GRAPH_NAME`
explicitly, reproducing byte-identical failure-message output. This issue's
own new real-graph verification test passes `graph_name="policy_system"`
instead, so a failing assertion correctly names the graph actually checked.

Non-test-collected: pytest's default `python_files` pattern (`test_*.py`,
this repo's root `pyproject.toml`) never matches this filename regardless of
the leading underscore -- the underscore is a human-readable convention
mirroring this same package's existing `_fakes.py`, signalling "shared
support module, not a test module" rather than being the collection-blocking
mechanism itself.

`_snapshot_node_properties`/`_snapshot_edge_properties`/`_snapshot_edge_ids`
are NEW (issue #28, CHANGES.md #1) -- #16's own capstone never needed
property-level or edge-existence snapshots, only counts/embeddings. Used by
this issue's live-merge runner (AC-BI-006's before/after integrity proof)
and by `test_live_merge_assertions.py`. Scoped per a direct read of
`ps_service/company_merge/graph_writer.py`:

- `persist_rewired_edges` (graph_writer.py ~443-499) writes `HAS`/
  `SATISFIED_BY`/`REQUIRES` via a bare `MATCH ... MERGE (s)-[:TYPE]->(t)`
  with no `SET` clause at all -- confirmed these three relationship types
  never carry any edge property. Existence-survival only.
- `_upsert_provenance_edge` (graph_writer.py ~159-185), used for `DEFINES`/
  `EXPRESSES`, always issues `SET e.source_ref = $source_ref` -- confirmed
  these two DO carry properties, so they get a real property-level snapshot.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from ps_service.company_merge.falkordb_client import GraphHandle

_NODE_LABELS = ("RegulatoryInstrument", "Role", "Requirement", "Obligation", "Capability")
_EDGE_TYPES = ("DEFINES", "EXPRESSES", "HAS", "SATISFIED_BY", "REQUIRES")

# graph_writer.py-confirmed split (module docstring above): only these two
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
    """Per-label node counts + per-relationship-type edge counts -- AC-005's
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
    AC-005's "zero further embedding-backfill writes" proof compares this
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
    """Per-label `{id: properties}` for every node label this issue's
    before/after property-level integrity proof needs (issue #28,
    CHANGES.md #1): `RegulatoryInstrument`/`Role`/`Requirement`/`Obligation`/
    `Capability`.

    The `embedding` key is stripped at CAPTURE time (not comparison time)
    for `Capability` -- a semantic match's embedding can legitimately be
    backfilled between two snapshots (AC-005's own already-proven backfill
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
    only two relationship types that carry any (issue #28, CHANGES.md #1) --
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
    three relationship types that carry NO properties (issue #28,
    CHANGES.md #1) -- `HAS`/`SATISFIED_BY`/`REQUIRES`, confirmed against
    `graph_writer.persist_rewired_edges`'s bare `MERGE (s)-[:TYPE]->(t)`
    (no `SET` clause at all). Existence-survival only: AC-BI-006's
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
