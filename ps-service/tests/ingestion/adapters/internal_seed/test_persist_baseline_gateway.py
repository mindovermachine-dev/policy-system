"""The internal-seed baseline graph goes through the gateway (#208, slices S3 and S4).

`{short}_baseline` receives the minted spine as one gateway group submitted after the native
group: first the RegulatoryInstrument, then Role, Capability, Policy, Standard and Control (the
labels other nodes and edges point at), then Obligation and Requirement, then PracticeArea and
RiskPath, then every edge with canonical endpoint ids.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from ingestion.adapters.internal_seed._seeds import (
    BASELINE_GRAPH,
    NATIVE_GRAPH,
    Run,
    full_seed,
    node,
)
from ps_service.domain_mapper.identity import (
    capability_id,
    control_id,
    policy_id,
    role_id,
    standard_id,
)
from ps_service.graph_gateway.models import UpsertNode
from ps_service.ingestion.adapters.internal_seed.errors import InternalSeedError
from ps_service.ingestion.adapters.internal_seed.models import InternalRegulationSeed
from ps_service.ingestion.adapters.internal_seed.persist import (
    _upsert_edge,  # pyright: ignore[reportPrivateUsage]
    _without_dangling_edges,  # pyright: ignore[reportPrivateUsage]
)

if TYPE_CHECKING:
    from graph_gateway._fakes import GatewayRig

_ROLE = role_id("Role A", "TESTREG-1.0")
_CAPABILITY = capability_id("Cap One")
_POLICY = policy_id("Access Policy")
_STANDARD = standard_id(_POLICY, "Access Standard")
_CONTROL = control_id(_STANDARD, "Access Control")


def test_reference_nodes_are_minted_into_one_baseline_group_in_a_fixed_order(
    rig: GatewayRig,
) -> None:
    run = Run(rig)

    run.ingest(full_seed())

    assert run.baseline.upsert_order[:6] == [
        ("RegulatoryInstrument", "TESTREG-1.0"),
        ("Role", _ROLE),
        ("Capability", _CAPABILITY),
        ("Policy", _POLICY),
        ("Standard", _STANDARD),
        ("Control", _CONTROL),
    ]
    assert run.baseline.nodes[("Role", _ROLE)] == {"name": "Role A", "confidence": 1.0}
    assert run.baseline.nodes[("Capability", _CAPABILITY)] == {
        "name": "Cap One",
        "confidence": 1.0,
    }
    assert run.baseline.nodes[("Policy", _POLICY)] == {"title": "Access Policy", "status": "draft"}
    assert run.baseline.nodes[("Standard", _STANDARD)] == {
        "title": "Access Standard",
        "implementation_status": "draft",
        "status": "draft",
    }
    assert run.baseline.nodes[("Control", _CONTROL)] == {
        "type": "detective",
        "title": "Access Control",
        "implementation_status": "draft",
        "status": "draft",
    }
    assert run.baseline.nodes[("RegulatoryInstrument", "TESTREG-1.0")] == {
        "title": "Test Regulation",
        "source_type": "internal",
        "effective_date": "2026-01-01",
        "version": "1.0",
        "status": "active",
    }


def test_native_group_is_submitted_first_then_the_baseline_group_under_one_audit_event(
    rig: GatewayRig,
) -> None:
    run = Run(rig)

    run.ingest(full_seed())

    assert [(group.graph, group.audit_event_id) for group in rig.store.groups] == [
        (NATIVE_GRAPH, run.audit_event_id),
        (BASELINE_GRAPH, run.audit_event_id),
    ]


def test_a_smaller_property_map_on_reingest_keeps_the_other_baseline_properties(
    rig: GatewayRig,
) -> None:
    run = Run(rig)
    run.ingest(full_seed(capability_properties={"name": "Cap One", "description": "kept"}))

    run.ingest(full_seed())

    assert run.baseline.nodes[("Capability", _CAPABILITY)] == {
        "name": "Cap One",
        "confidence": 1.0,
        "description": "kept",
    }


def test_reingesting_the_same_seed_adds_no_baseline_reference_entry(rig: GatewayRig) -> None:
    run = Run(rig)
    run.ingest(full_seed())
    logged = len(rig.store.entries[BASELINE_GRAPH])

    run.ingest(full_seed())

    assert len(rig.store.entries[BASELINE_GRAPH]) == logged
    assert len(rig.store.groups) == 2


def test_an_unstorable_baseline_property_rejects_the_document_before_any_submit(
    rig: GatewayRig,
) -> None:
    run = Run(rig)
    seed = full_seed(role_properties={"name": "Role A", "confidence": float("nan")})

    with pytest.raises(InternalSeedError, match="Role"):
        run.ingest(seed)

    assert rig.store.groups == []  # not even the (valid) native group


# --- S4: dependent and classification nodes, baseline edges ---


def test_the_whole_baseline_is_one_group_in_a_fixed_order(
    rig: GatewayRig,
) -> None:
    run = Run(rig)

    run.ingest(full_seed())

    labels = [label for label, _ in run.baseline.upsert_order]
    assert labels == [
        "RegulatoryInstrument",
        "Role",
        "Capability",
        "Policy",
        "Standard",
        "Control",
        "Obligation",
        "Requirement",
        "PracticeArea",
        "RiskPath",
    ]
    baseline_groups = [g for g in run.submitter.groups if g.graph == BASELINE_GRAPH]
    assert len(baseline_groups) == 1
    ops = [primitive.op for primitive in baseline_groups[0].primitives]
    assert ops == ["upsert_node"] * 10 + ["upsert_edge"] * 12  # all nodes, then all edges


def test_baseline_edges_use_canonical_ids_and_keep_source_ref_only_on_defines_and_expresses(
    rig: GatewayRig,
) -> None:
    run = Run(rig)

    run.ingest(full_seed())

    edges = {(key[0], key[2], key[4]): dict(value) for key, value in run.baseline.edges.items()}
    requirement = next(i for (label, i) in run.baseline.nodes if label == "Requirement")
    obligation = next(i for (label, i) in run.baseline.nodes if label == "Obligation")
    assert edges[("DEFINES", "TESTREG-1.0", _ROLE)] == {
        "identity": f"DEFINES:TESTREG-1.0->{_ROLE}",
        "source_ref": "Art. 1",
    }
    assert edges[("EXPRESSES", "TESTREG-1.0", requirement)]["source_ref"] == "Art. 2"
    assert edges[("HAS", _ROLE, obligation)] == {"identity": f"HAS:{_ROLE}->{obligation}"}
    assert edges[("REQUIRES", obligation, _CAPABILITY)] == {
        "identity": f"REQUIRES:{obligation}->{_CAPABILITY}"
    }
    assert edges[("IMPLEMENTED_BY", _STANDARD, _CONTROL)] == {
        "identity": f"IMPLEMENTED_BY:{_STANDARD}->{_CONTROL}"
    }
    assert len(edges) == 12


def test_a_requirement_with_one_bearing_role_gets_role_id_and_never_a_null(
    rig: GatewayRig,
) -> None:
    run = Run(rig)

    run.ingest(full_seed())

    (requirement,) = [p for (label, _), p in run.baseline.nodes.items() if label == "Requirement"]
    assert requirement["role_id"] == _ROLE
    assert None not in requirement.values()


def test_an_unstorable_dependent_or_classification_property_rejects_before_any_submit(
    rig: GatewayRig,
) -> None:
    run = Run(rig)
    seed = full_seed()
    broken = InternalRegulationSeed(
        nodes=tuple(
            node("Obligation", "obl-a", text="Do A", confidence=float("nan"))
            if n.label == "Obligation"
            else n
            for n in seed.nodes
        ),
        edges=seed.edges,
    )

    with pytest.raises(InternalSeedError, match="Obligation"):
        run.ingest(broken)

    assert rig.store.groups == []


def test_the_dangling_edge_filter_applies_to_the_baseline_nodes_too() -> None:
    nodes = (UpsertNode(label="Role", id="r1"), UpsertNode(label="Capability", id="c1"))
    kept = _upsert_edge("COVERS", ("Role", "r1"), ("Capability", "c1"), {})
    dropped = _upsert_edge("COVERS", ("Role", "r1"), ("Capability", "gone"), {})

    assert _without_dangling_edges(nodes, (kept, dropped)) == (kept,)
