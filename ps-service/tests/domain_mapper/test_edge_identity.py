"""Edge identity of the Domain Mapper's baseline writes.

Every DEFINES, EXPRESSES, HAS, SATISFIED_BY and REQUIRES edge the stages submit carries the
identity `{type}:{source}->{target}`, the scheme of the internal-seed path (whose
`edge_identity` is imported here so the two cannot drift). The gateway merges edges on that
identity, so re-running the stages adds no parallel edge, and a baseline graph written before the
gateway (edges without an identity) has its edges stamped, not duplicated.
"""

from __future__ import annotations

from collections import Counter
from typing import TYPE_CHECKING, cast

from ingestion.adapters.internal_seed._legacy_reference import LegacyRecordingGraph

from domain_mapper._pipeline import run_derivation, run_extraction
from ps_service.graph_gateway.digest import canonical_digest
from ps_service.graph_gateway.label_allow_list import (
    ALLOWED_NODE_LABELS,
    ALLOWED_RELATIONSHIP_TYPES,
    require_allowed_node_label,
    require_allowed_relationship_type,
)
from ps_service.ingestion.adapters.internal_seed.reconcile import edge_identity

if TYPE_CHECKING:
    from domain_mapper._fakes import MakeEmitter
    from domain_mapper._gateway_fakes import BaselineRig
    from ps_service.graph_gateway.models import GraphLogEntry
    from ps_service.logging import LogEmitter

_EDGE_TYPES = {"DEFINES", "EXPRESSES", "HAS", "SATISFIED_BY", "REQUIRES"}
_NODE_LABELS = {"RegulatoryInstrument", "Role", "Requirement", "Obligation", "Capability"}


def _run_pipeline(rig: BaselineRig, emitter: LogEmitter) -> None:
    run_extraction(rig, emitter)
    run_derivation(rig, emitter)


def _endpoint(entry: GraphLogEntry, side: str) -> dict[str, str]:
    endpoint = cast("dict[str, str]", entry.content[side])
    return {"label": endpoint["label"], "id": endpoint["id"]}


def _edge_entries(rig: BaselineRig) -> list[GraphLogEntry]:
    return [entry for entry in rig.logged() if entry.name in _EDGE_TYPES]


def test_each_edge_type_carries_type_source_arrow_target_identity(
    make_emitter: MakeEmitter, baseline_rig: BaselineRig
) -> None:
    """All five edge types, identity equal to the internal-seed scheme's output."""
    emitter, _ = make_emitter()

    _run_pipeline(baseline_rig, emitter)

    edges = _edge_entries(baseline_rig)
    assert {entry.name for entry in edges} == _EDGE_TYPES
    for entry in edges:
        source_id = _endpoint(entry, "source")["id"]
        target_id = _endpoint(entry, "target")["id"]
        assert entry.identity == edge_identity(entry.name, source_id, target_id)
        assert entry.identity == f"{entry.name}:{source_id}->{target_id}"
    stored = {key[5] for key in baseline_rig.baseline.edges}
    assert stored == {entry.identity for entry in edges}


def test_rerunning_the_stages_creates_no_parallel_edge(
    make_emitter: MakeEmitter, baseline_rig: BaselineRig
) -> None:
    """The second extract + derive merges into the existing edges: same edges, nothing logged."""
    emitter, _ = make_emitter()
    _run_pipeline(baseline_rig, emitter)
    edges = Counter(
        (key[0], key[2], key[4]) for key in baseline_rig.baseline.edges
    )  # (type, source id, target id)
    digest = canonical_digest(baseline_rig.baseline)
    entries = baseline_rig.logged()
    groups = list(baseline_rig.groups)

    _run_pipeline(baseline_rig, emitter)

    assert set(edges.values()) == {1}
    assert Counter((key[0], key[2], key[4]) for key in baseline_rig.baseline.edges) == edges
    assert canonical_digest(baseline_rig.baseline) == digest
    assert baseline_rig.logged() == entries
    assert baseline_rig.groups == groups


def _legacy_copy_of(rig: BaselineRig) -> LegacyRecordingGraph:
    """What the pre-gateway Cypher (`MERGE ... SET n += $properties`, `SET r += ...`) built.

    The nodes and edges `rig` holds are replayed through the frozen legacy query shapes against
    the legacy recording graph, which keeps edges without an identity.
    """
    legacy = LegacyRecordingGraph()
    for (label, node_id), properties in rig.baseline.nodes.items():
        legacy.query(
            f"MERGE (n:{label} {{id: $id}}) SET n += $properties",
            {"id": node_id, "properties": dict(properties)},
        )
    for (
        edge_type,
        source_label,
        source_id,
        target_label,
        target_id,
        _,
    ), properties in rig.baseline.edges.items():
        legacy.query(
            f"MATCH (a:{source_label} {{id: $from_id}}), (b:{target_label} {{id: $to_id}}) "
            f"MERGE (a)-[r:{edge_type}]->(b) SET r += $properties",
            {
                "from_id": source_id,
                "to_id": target_id,
                "properties": {k: v for k, v in properties.items() if k != "identity"},
            },
        )
    return legacy


def test_a_legacy_baseline_graph_has_its_identityless_edges_stamped_not_duplicated(
    make_emitter: MakeEmitter, baseline_rig: BaselineRig, other_baseline_rig: BaselineRig
) -> None:
    """DEFINES, EXPRESSES, HAS, SATISFIED_BY and REQUIRES written before the gateway.

    The reference rig runs the stages on an empty graph. The legacy graph is that same content in
    the legacy writer's shape (no `identity` on any edge). Running the stages on it stamps every
    legacy edge in ONE stamp group under the run's audit event, adds no edge, and ends at the
    reference digest.
    """
    emitter, _ = make_emitter()
    _run_pipeline(other_baseline_rig, emitter)
    legacy = _legacy_copy_of(other_baseline_rig)
    assert {key[0] for key in legacy.edges} == _EDGE_TYPES
    for (label, node_id), properties in legacy.nodes.items():
        baseline_rig.baseline.seed_node((label,), node_id, properties)
    for key, properties in legacy.edges.items():
        baseline_rig.baseline.seed_legacy_edge(key, properties)
    legacy_edge_count = len(baseline_rig.baseline.edges)
    assert {key[5] for key in baseline_rig.baseline.edges} == {""}

    _run_pipeline(baseline_rig, emitter)

    assert len(baseline_rig.baseline.edges) == legacy_edge_count
    assert all(
        key[5] == edge_identity(key[0], key[2], key[4]) for key in baseline_rig.baseline.edges
    )
    stamp_groups = [
        group
        for group in baseline_rig.groups
        if any(entry.content["op"] == "stamp_edge_identity" for entry in group.entries)
    ]
    [stamp_group] = stamp_groups
    assert stamp_group.audit_event_id == baseline_rig.audit_event_id
    assert {entry.name for entry in stamp_group.entries} == _EDGE_TYPES
    assert len(stamp_group.entries) == legacy_edge_count
    assert canonical_digest(baseline_rig.baseline) == canonical_digest(other_baseline_rig.baseline)


def test_all_five_edge_types_and_their_labels_are_gateway_allow_listed(
    make_emitter: MakeEmitter, baseline_rig: BaselineRig
) -> None:
    """The names the stages write are on the gateway allow-list."""
    emitter, _ = make_emitter()
    _run_pipeline(baseline_rig, emitter)

    assert _EDGE_TYPES <= ALLOWED_RELATIONSHIP_TYPES
    assert _NODE_LABELS <= ALLOWED_NODE_LABELS
    for edge_type in sorted(_EDGE_TYPES):
        assert require_allowed_relationship_type(edge_type) == edge_type
    for label in sorted(_NODE_LABELS):
        assert require_allowed_node_label(label) == label
    written_labels = {label for label, _ in baseline_rig.baseline.nodes}
    written_labels |= {key[1] for key in baseline_rig.baseline.edges}
    written_labels |= {key[3] for key in baseline_rig.baseline.edges}
    assert written_labels == _NODE_LABELS
    assert {key[0] for key in baseline_rig.baseline.edges} == _EDGE_TYPES
