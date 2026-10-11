"""A resubmitted internal-seed document retires what it no longer contains (GH #99, S1 and S2).

The Capability rename of issue #95: the old Capability node (and so its edges) must be gone from
both `{short}_native` and `{short}_baseline` once the resubmission completes, the new one must
stay, and each retired edge is deleted explicitly before the node that carried it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
import redis.exceptions

from ingestion.adapters.internal_seed._seeds import (
    BASELINE_GRAPH,
    NATIVE_GRAPH,
    RI_REF,
    RecordingSubmitter,
    Run,
    full_seed,
)
from ps_service.domain_mapper.identity import capability_id
from ps_service.graph_gateway.models import (
    DeleteEdge,
    DeleteNode,
    MutationGroup,
    StampEdgeIdentity,
    UpsertEdge,
    UpsertNode,
)
from ps_service.ingestion.adapters.internal_seed.errors import InternalSeedError
from ps_service.ingestion.adapters.internal_seed.models import InternalRegulationSeed, SeedRef

if TYPE_CHECKING:
    from graph_gateway._fakes import GatewayRig

    from ps_service.graph_gateway.graph_reader import PersistedElements

_OLD_BASELINE = ("Capability", capability_id("Cap One"))
_NEW_BASELINE = ("Capability", capability_id("Cap Two"))
_OLD_NATIVE = ("Capability", "cap-1")
_NEW_NATIVE = ("Capability", "cap-2")
_INCIDENT_EDGE_TYPES = {"REQUIRES", "GOVERNED_BY", "COVERS", "MITIGATED_BY"}


def renamed_capability_seed() -> InternalRegulationSeed:
    """`full_seed()` with Capability `cap-1` "Cap One" renamed to `cap-2` "Cap Two"."""
    seed = full_seed()

    def moved(ref: SeedRef) -> SeedRef:
        return ref.model_copy(update={"id": "cap-2"}) if ref.id == "cap-1" else ref

    nodes = tuple(
        node.model_copy(update={"id": "cap-2", "properties": {"name": "Cap Two"}})
        if node.id == "cap-1"
        else node
        for node in seed.nodes
    )
    edges = tuple(
        edge.model_copy(update={"from_": moved(edge.from_), "to": moved(edge.to)})
        for edge in seed.edges
    )
    return InternalRegulationSeed(nodes=nodes, edges=edges)


def _rename_cycle(rig: GatewayRig) -> Run:
    Run(rig).ingest(full_seed())
    rename = Run(rig)
    rename.ingest(renamed_capability_seed())
    return rename


def _retire_group(run: Run, graph: str) -> MutationGroup:
    return next(group for group in run.submitter.groups if group.graph == graph)


def test_renaming_a_capability_removes_the_old_node_from_both_graphs(rig: GatewayRig) -> None:
    run = _rename_cycle(rig)

    assert _OLD_NATIVE not in run.native.nodes
    assert _NEW_NATIVE in run.native.nodes
    assert _OLD_BASELINE not in run.baseline.nodes
    assert _NEW_BASELINE in run.baseline.nodes


def test_no_edge_of_the_retired_capability_survives_in_either_graph(rig: GatewayRig) -> None:
    run = _rename_cycle(rig)

    for graph, old in ((run.native, _OLD_NATIVE), (run.baseline, _OLD_BASELINE)):
        assert not [key for key in graph.edges if old in {key[1:3], key[3:5]}]


def test_retiring_a_node_deletes_each_incident_edge_before_the_node(rig: GatewayRig) -> None:
    run = _rename_cycle(rig)

    for graph in (run.baseline_name, run.native_name):
        ops = [primitive.op for primitive in _retire_group(run, graph).primitives]
        deleted_edges = [i for i, op in enumerate(ops) if op == "delete_edge"]
        (deleted_node,) = [i for i, op in enumerate(ops) if op == "delete_node"]
        assert len(deleted_edges) == len(_INCIDENT_EDGE_TYPES)
        assert max(deleted_edges) < deleted_node
        assert deleted_node > max(
            i for i, op in enumerate(ops) if op in {"upsert_node", "upsert_edge"}
        )


def test_each_retired_edge_has_a_log_entry_with_type_identity_graph_and_audit_event(
    rig: GatewayRig,
) -> None:
    run = _rename_cycle(rig)

    groups = [
        group
        for group in rig.store.read_groups_by_audit_event(run.audit_event_id)
        if group.graph == run.baseline_name
    ]
    entries = [entry for group in groups for entry in group.entries]
    deleted = [entry for entry in entries if entry.content["op"] == "delete_edge"]
    assert {entry.name for entry in deleted} == _INCIDENT_EDGE_TYPES
    assert all(entry.identity and "cap" in entry.identity for entry in deleted)
    (node_entry,) = [entry for entry in entries if entry.content["op"] == "delete_node"]
    assert (node_entry.name, node_entry.identity) == _OLD_BASELINE
    assert all(group.audit_event_id == run.audit_event_id for group in groups)


def test_dropped_edge_between_survivors_is_deleted_without_a_node_delete(rig: GatewayRig) -> None:
    Run(rig).ingest(full_seed())
    seed = full_seed()
    kept = tuple(edge for edge in seed.edges if edge.type != "COVERS")
    second = Run(rig)

    second.ingest(InternalRegulationSeed(nodes=seed.nodes, edges=kept))

    for graph in (second.baseline_name, second.native_name):
        primitives = _retire_group(second, graph).primitives
        assert [p.type for p in primitives if isinstance(p, DeleteEdge)] == ["COVERS"]
        assert not [p for p in primitives if isinstance(p, DeleteNode)]
    assert not [key for key in second.baseline.edges if key[0] == "COVERS"]
    assert not [key for key in second.native.edges if key[0] == "COVERS"]
    assert len(second.baseline.nodes) == len(full_seed().nodes)


def test_submitted_primitives_precede_the_retirements(rig: GatewayRig) -> None:
    run = _rename_cycle(rig)

    primitives = _retire_group(run, run.baseline_name).primitives
    first_delete = next(i for i, p in enumerate(primitives) if isinstance(p, DeleteEdge))
    assert all(isinstance(p, UpsertNode | UpsertEdge) for p in primitives[:first_delete])


def test_resubmitting_an_unchanged_document_deletes_nothing_and_logs_nothing(
    rig: GatewayRig,
) -> None:
    Run(rig).ingest(full_seed())
    logged = len(rig.store.groups)
    again = Run(rig)

    again.ingest(full_seed())

    assert len(rig.store.groups) == logged
    assert not [
        p
        for group in again.submitter.groups
        for p in group.primitives
        if isinstance(p, DeleteNode | DeleteEdge | StampEdgeIdentity)
    ]


def test_a_submission_that_only_adds_nodes_retires_nothing(rig: GatewayRig) -> None:
    Run(rig).ingest(full_seed())
    seed = full_seed()
    extra = seed.nodes[0].model_copy(
        update={"label": "Role", "id": "role-b", "properties": {"name": "Role B"}}
    )
    adding = Run(rig)

    adding.ingest(InternalRegulationSeed(nodes=(*seed.nodes, extra), edges=seed.edges))

    assert not [
        p
        for group in adding.submitter.groups
        for p in group.primitives
        if isinstance(p, DeleteNode | DeleteEdge)
    ]


def test_foreign_label_and_edge_type_in_target_graphs_survive_a_rename(rig: GatewayRig) -> None:
    run = Run(rig)
    run.ingest(full_seed())
    for graph in (run.native, run.baseline):
        graph.seed_node(("ReingestProgress",), "marker-1", {"state": "x"})
        graph.seed_legacy_edge(("MERGED_INTO", *RI_REF, "ReingestProgress", "marker-1"), {})
    rename = Run(rig)

    rename.ingest(renamed_capability_seed())

    for graph in (rename.native, rename.baseline):
        assert ("ReingestProgress", "marker-1") in graph.nodes
        assert any(key[0] == "MERGED_INTO" for key in graph.edges)


def test_an_identity_less_edge_the_document_dropped_is_stamped_then_deleted(
    rig: GatewayRig,
) -> None:
    run = Run(rig)
    run.ingest(full_seed())
    stray = ("COVERS", "PracticeArea", "pa-1", "Capability", "cap-1")
    run.native.seed_legacy_edge(stray, {})
    seed = full_seed()
    seed = InternalRegulationSeed(
        nodes=seed.nodes, edges=tuple(e for e in seed.edges if e.type != "COVERS")
    )
    again = Run(rig)

    again.ingest(seed)

    assert not [key for key in again.native.edges if key[0] == "COVERS"]


def test_a_baseline_graph_behind_its_log_rejects_before_the_native_graph_is_written(
    rig: GatewayRig,
) -> None:
    run = Run(rig)
    run.ingest(full_seed())
    rig.store.report_head_extra = 1  # the baseline log claims an entry the graph does not hold
    logged = len(rig.store.groups)

    with pytest.raises(InternalSeedError, match=r"^FalkorDB write failed: "):
        Run(rig).ingest(renamed_capability_seed())

    assert len(rig.store.groups) == logged


def test_a_failed_read_of_the_baseline_graph_writes_nothing(rig: GatewayRig) -> None:
    Run(rig).ingest(full_seed())
    logged = len(rig.store.groups)
    rig.graphs.open(BASELINE_GRAPH).fail_on_read(redis.exceptions.ConnectionError("down"))

    with pytest.raises(InternalSeedError, match=r"^FalkorDB write failed: "):
        Run(rig).ingest(renamed_capability_seed())

    assert len(rig.store.groups) == logged
    assert _OLD_NATIVE in rig.graphs.open(NATIVE_GRAPH).nodes


class _WriterBetweenReadAndSubmit(RecordingSubmitter):
    """Logs a group on the baseline graph right after the reconcile read of it."""

    def __init__(self, rig: GatewayRig) -> None:
        super().__init__(rig.gateway)
        self._rig = rig

    def read_persisted_elements(self, graph: str) -> PersistedElements:
        elements = super().read_persisted_elements(graph)
        if graph == BASELINE_GRAPH:
            self._rig.gateway.submit_group(
                MutationGroup(
                    graph=graph,
                    audit_event_id=next(iter(self._rig.store.audit_rows)),
                    primitives=(UpsertNode(label="Role", id="intruder"),),
                )
            )
        return elements


def test_a_writer_between_the_read_and_the_submit_is_refused_and_nothing_is_retired(
    rig: GatewayRig,
) -> None:
    Run(rig).ingest(full_seed())
    run = Run(rig)
    run.submitter = _WriterBetweenReadAndSubmit(rig)

    with pytest.raises(InternalSeedError, match=r"^FalkorDB write failed: "):
        run.ingest(renamed_capability_seed())

    assert _OLD_BASELINE in run.baseline.nodes


def test_a_node_with_several_labels_rejects_the_submission_with_internal_seed_error(
    rig: GatewayRig,
) -> None:
    run = Run(rig)
    run.ingest(full_seed())
    run.baseline.seed_node(("Capability", "Extra"), "odd", {})
    logged = len(rig.store.groups)

    with pytest.raises(InternalSeedError, match=r"^FalkorDB write failed: "):
        Run(rig).ingest(renamed_capability_seed())

    assert len(rig.store.groups) == logged
