"""Reconciliation stays inside the target's graphs and takes nothing from the submission.

It is also auditable and replayable.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from pydantic import ValidationError

from ingestion.adapters.internal_seed._seeds import Run, full_seed
from ingestion.adapters.internal_seed.test_reconcile_retirement import (
    renamed_capability_seed,
)
from ps_service.domain_mapper.identity import capability_id
from ps_service.graph_gateway.digest import canonical_digest
from ps_service.graph_gateway.models import DeleteNode
from ps_service.ingestion.adapters.internal_seed.models import (
    InternalRegulationSeed,
    SeedNode,
    SeedRef,
)

if TYPE_CHECKING:
    from graph_gateway._fakes import GatewayRig


def _other_instrument_seed() -> InternalRegulationSeed:
    """`full_seed()` re-homed under another short name, so it lands in other graphs."""
    seed = full_seed()

    def moved(ref: SeedRef) -> SeedRef:
        return ref.model_copy(update={"id": "OTHERREG-1.0"}) if ref.id == "TESTREG-1.0" else ref

    nodes = tuple(
        n.model_copy(update={"id": "OTHERREG-1.0"}) if n.id == "TESTREG-1.0" else n
        for n in seed.nodes
    )
    edges = tuple(
        e.model_copy(update={"from_": moved(e.from_), "to": moved(e.to)}) for e in seed.edges
    )
    return InternalRegulationSeed(nodes=nodes, edges=edges)


def test_retirement_reads_and_writes_only_the_target_instruments_graphs(rig: GatewayRig) -> None:
    other = Run(rig)
    other.ingest(_other_instrument_seed())
    digests = (canonical_digest(other.native), canonical_digest(other.baseline))
    queries = (len(other.native.queries), len(other.baseline.queries))
    logged = {g for g, _ in rig.store.checkpoints} | set(rig.store.logged_graphs())
    Run(rig).ingest(full_seed())

    Run(rig).ingest(renamed_capability_seed())

    assert (len(other.native.queries), len(other.baseline.queries)) == queries
    assert (canonical_digest(other.native), canonical_digest(other.baseline)) == digests
    assert set(rig.store.logged_graphs()) == logged | {"testreg_native", "testreg_baseline"}


def test_a_field_naming_a_node_to_retire_is_rejected_and_deletes_nothing(rig: GatewayRig) -> None:
    run = Run(rig)
    run.ingest(full_seed())
    logged = len(rig.store.groups)

    with pytest.raises(ValidationError):
        SeedNode.model_validate(
            {"label": "Capability", "id": "x", "properties": {}, "supersedes": "cap-1"}
        )
    with pytest.raises(ValidationError):
        InternalRegulationSeed.model_validate({"nodes": [], "edges": [], "supersedes": ["cap-1"]})

    assert len(rig.store.groups) == logged
    assert ("Capability", "cap-1") in run.native.nodes


def test_a_property_value_naming_another_node_does_not_steer_a_delete(rig: GatewayRig) -> None:
    Run(rig).ingest(full_seed())
    seed = full_seed()
    nodes = tuple(
        n.model_copy(update={"properties": {**n.properties, "note": "supersedes pol-1"}})
        if n.label == "Capability"
        else n
        for n in seed.nodes
    )
    again = Run(rig)

    again.ingest(InternalRegulationSeed(nodes=nodes, edges=seed.edges))

    assert not [
        p for g in again.submitter.groups for p in g.primitives if isinstance(p, DeleteNode)
    ]
    assert ("Policy", "pol-1") in again.native.nodes


def test_each_retirement_is_logged_with_label_id_graph_and_audit_event_in_both_graphs(
    rig: GatewayRig,
) -> None:
    Run(rig).ingest(full_seed())
    rename = Run(rig)
    rename.ingest(renamed_capability_seed())

    groups = rig.store.read_groups_by_audit_event(rename.audit_event_id)

    assert {g.graph for g in groups} == {rename.native_name, rename.baseline_name}
    for graph, old_id in (
        (rename.native_name, "cap-1"),
        (rename.baseline_name, capability_id("Cap One")),
    ):
        mine = [g for g in groups if g.graph == graph]
        assert all(g.audit_event_id == rename.audit_event_id for g in mine)
        entries = [e for g in mine for e in g.entries]
        node_deletes = [e for e in entries if e.content["op"] == "delete_node"]
        assert [(e.name, e.identity) for e in node_deletes] == [("Capability", old_id)]
        edge_deletes = [e for e in entries if e.content["op"] == "delete_edge"]
        assert {e.name for e in edge_deletes} == {
            "REQUIRES",
            "GOVERNED_BY",
            "COVERS",
            "MITIGATED_BY",
        }
        assert all(e.identity for e in edge_deletes)


def test_replaying_the_log_after_a_rename_cycle_rebuilds_the_live_graph(rig: GatewayRig) -> None:
    Run(rig).ingest(full_seed())
    rename = Run(rig)
    rename.ingest(renamed_capability_seed())
    live = (canonical_digest(rename.native), canonical_digest(rename.baseline))
    rename.native.flush()
    rename.baseline.flush()

    gateway = rig.restart()
    gateway.replay_graph(rename.native_name)
    gateway.replay_graph(rename.baseline_name)

    assert (canonical_digest(rename.native), canonical_digest(rename.baseline)) == live
    assert ("Capability", "cap-1") not in rename.native.nodes
    assert ("Capability", "cap-2") in rename.native.nodes
