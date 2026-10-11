"""Whole-document reconciliation across labels and rename cycles (GH #99, S3).

A Policy rename strands its Standard/Control subtree, every label the seed carries can be dropped,
a rename-and-retire cycle leaves exactly what a fresh ingest of the new document leaves, and an
unchanged or add-only resubmission retires nothing.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from graph_gateway._fakes import GatewayRig

from ingestion.adapters.internal_seed._seeds import Run, full_seed
from ps_service.domain_mapper.identity import control_id, policy_id, standard_id
from ps_service.graph_gateway.digest import canonical_digest
from ps_service.graph_gateway.models import DeleteEdge, DeleteNode, StampEdgeIdentity
from ps_service.ingestion.adapters.internal_seed.models import (
    InternalRegulationSeed,
    SeedEdge,
    SeedNode,
    SeedRef,
)

if TYPE_CHECKING:
    from collections.abc import Callable

_DROPPABLE = (
    "Role",
    "Obligation",
    "Requirement",
    "Capability",
    "Policy",
    "Standard",
    "Control",
    "PracticeArea",
    "RiskPath",
)


def _renamed(seed: InternalRegulationSeed, old: str, new: str, **changes: str) -> SeedNode:
    return next(n for n in seed.nodes if n.id == old).model_copy(
        update={
            "id": new,
            "properties": {**next(n for n in seed.nodes if n.id == old).properties, **changes},
        }
    )


def renamed_policy_seed() -> InternalRegulationSeed:
    """`full_seed()` with Policy `pol-1` "Access Policy" renamed (new id), subtree re-pointed."""
    seed = full_seed()

    def moved(ref: SeedRef) -> SeedRef:
        return ref.model_copy(update={"id": "pol-2"}) if ref.id == "pol-1" else ref

    nodes = tuple(
        _renamed(seed, "pol-1", "pol-2", title="Renamed Policy") if n.id == "pol-1" else n
        for n in seed.nodes
    )
    edges = tuple(
        e.model_copy(update={"from_": moved(e.from_), "to": moved(e.to)}) for e in seed.edges
    )
    return InternalRegulationSeed(nodes=nodes, edges=edges)


_DEPENDENTS = {
    "Role": ("Obligation",),
    "Policy": ("Standard", "Control"),
    "Standard": ("Control",),
}


def without(seed: InternalRegulationSeed, label: str) -> InternalRegulationSeed:
    """`seed` minus every node of `label` (and the weak entities that need it), and their edges."""
    dropped = {label, *_DEPENDENTS.get(label, ())}
    gone = {n.id for n in seed.nodes if n.label in dropped}
    return InternalRegulationSeed(
        nodes=tuple(n for n in seed.nodes if n.id not in gone),
        edges=tuple(e for e in seed.edges if e.from_.id not in gone and e.to.id not in gone),
    )


def _ids(run: Run) -> tuple[set[tuple[str, str]], set[tuple[str, str]]]:
    return set(run.native.nodes), set(run.baseline.nodes)


def _dangling(run: Run) -> list[tuple[str, ...]]:
    return [
        key
        for graph in (run.native, run.baseline)
        for key in graph.edges
        if (key[1], key[2]) not in graph.nodes or (key[3], key[4]) not in graph.nodes
    ]


def test_renaming_a_policy_retires_the_old_policy_standard_and_control(rig: GatewayRig) -> None:
    Run(rig).ingest(full_seed())
    old_policy = policy_id("Access Policy")
    old_standard = standard_id(old_policy, "Access Standard")
    old_control = control_id(old_standard, "Access Control")
    rename = Run(rig)

    rename.ingest(renamed_policy_seed())

    new_policy = policy_id("Renamed Policy")
    new_standard = standard_id(new_policy, "Access Standard")
    new_control = control_id(new_standard, "Access Control")
    baseline = rename.baseline.nodes
    for label, old, new in (
        ("Policy", old_policy, new_policy),
        ("Standard", old_standard, new_standard),
        ("Control", old_control, new_control),
    ):
        assert (label, old) not in baseline
        assert (label, new) in baseline
    assert ("Policy", "pol-1") not in rename.native.nodes
    assert ("Policy", "pol-2") in rename.native.nodes
    assert not _dangling(rename)


def test_policy_rename_edges_point_at_the_new_policy_only(rig: GatewayRig) -> None:
    Run(rig).ingest(full_seed())
    rename = Run(rig)

    rename.ingest(renamed_policy_seed())

    for graph in (rename.native, rename.baseline):
        governing = [k for k in graph.edges if k[0] in {"GOVERNED_BY", "SUPPORTED_BY", "OWNS"}]
        assert len(governing) == 3
        assert all("pol-1" not in k[1:5] for k in governing)


@pytest.mark.parametrize("label", _DROPPABLE)
def test_dropping_every_node_of_a_label_removes_them_and_their_edges(
    rig: GatewayRig, label: str
) -> None:
    Run(rig).ingest(full_seed())
    second = Run(rig)

    second.ingest(without(full_seed(), label))

    for nodes in _ids(second):
        assert not [key for key in nodes if key[0] == label]
    assert not _dangling(second)


@pytest.mark.parametrize("make", [renamed_policy_seed, lambda: without(full_seed(), "Control")])
def test_a_rename_cycle_leaves_the_digest_of_a_fresh_ingest_of_the_new_document(
    rig: GatewayRig, make: Callable[[], InternalRegulationSeed]
) -> None:
    Run(rig).ingest(full_seed())
    cycled = Run(rig)
    cycled.ingest(make())
    fresh_rig = GatewayRig()
    fresh = Run(fresh_rig)
    fresh.ingest(make())

    assert canonical_digest(cycled.native) == canonical_digest(fresh.native)
    assert canonical_digest(cycled.baseline) == canonical_digest(fresh.baseline)


def test_resubmitting_an_unchanged_document_logs_nothing_and_emits_no_retirement(
    rig: GatewayRig,
) -> None:
    Run(rig).ingest(full_seed())
    logged = len(rig.store.groups)
    again = Run(rig)

    again.ingest(full_seed())
    again.ingest(full_seed())

    assert len(rig.store.groups) == logged
    submitted = [p for g in again.submitter.groups for p in g.primitives]
    assert submitted
    assert not [p for p in submitted if isinstance(p, DeleteNode | DeleteEdge | StampEdgeIdentity)]


def test_a_submission_that_only_adds_nodes_keeps_everything_and_adds_them(rig: GatewayRig) -> None:
    Run(rig).ingest(full_seed())
    before = _ids(Run(rig))
    seed = full_seed()
    extra = SeedNode.model_validate(
        {"label": "Role", "id": "role-b", "properties": {"name": "Role B"}}
    )
    adding = Run(rig)

    adding.ingest(InternalRegulationSeed(nodes=(*seed.nodes, extra), edges=seed.edges))

    native, baseline = _ids(adding)
    assert before[0] < native
    assert before[1] < baseline
    assert not [
        p
        for g in adding.submitter.groups
        for p in g.primitives
        if isinstance(p, DeleteNode | DeleteEdge)
    ]


def test_resubmitting_under_a_new_version_id_retires_the_previous_version_subtree(
    rig: GatewayRig,
) -> None:
    """Characterization of issue #99: ownership is graph membership, so a version bump replaces.

    The graphs are per short name; `TESTREG-2.0` lands in the same `testreg_*` graphs and the
    submission is the whole document, so everything of `TESTREG-1.0` is retired.
    """
    first = full_seed()
    Run(rig).ingest(first)

    def bumped(ref: SeedRef) -> SeedRef:
        return ref.model_copy(update={"id": "TESTREG-2.0"}) if ref.id == "TESTREG-1.0" else ref

    nodes = tuple(
        n.model_copy(update={"id": "TESTREG-2.0", "properties": {**n.properties, "version": "2.0"}})
        if n.id == "TESTREG-1.0"
        else n
        for n in first.nodes
    )
    edges: tuple[SeedEdge, ...] = tuple(
        e.model_copy(update={"from_": bumped(e.from_), "to": bumped(e.to)}) for e in first.edges
    )
    bump = Run(rig)

    bump.ingest(InternalRegulationSeed(nodes=nodes, edges=edges))

    for graph in (bump.native, bump.baseline):
        instruments = [k for k in graph.nodes if k[0] == "RegulatoryInstrument"]
        assert len(instruments) == 1
        assert instruments[0][1].endswith("2.0")
