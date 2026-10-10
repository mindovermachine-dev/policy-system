"""The internal-seed native graph is written as one gateway group (#208, slice S2).

`{short}_native` receives the customer's raw submission verbatim (B5): every node in document
order, then every edge, as one mutation group logged under the run's audit event id. Postgres and
FalkorDB are the gateway rig's in-memory fakes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from ingestion.adapters.internal_seed._seeds import (
    CAPABILITY,
    NATIVE_GRAPH,
    OBLIGATION,
    RI,
    RI_REF,
    ROLE,
    Run,
    edge,
    node,
)
from ps_service.graph_gateway.models import MutationGroup
from ps_service.ingestion.adapters.internal_seed.errors import InternalSeedError
from ps_service.ingestion.adapters.internal_seed.models import (
    InternalRegulationSeed,
)
from ps_service.ingestion.adapters.internal_seed.persist import (
    build_native_primitives,
)

if TYPE_CHECKING:
    from graph_gateway._fakes import GatewayRig


def _seed(*, capability_properties: dict[str, str | float] | None = None) -> InternalRegulationSeed:
    properties = capability_properties if capability_properties is not None else {"name": "Cap One"}
    return InternalRegulationSeed(
        nodes=(
            RI,
            node("Role", "role-a", name="Role A"),
            node("Obligation", "obl-a", text="Do A"),
            node("Capability", "cap-1", **properties),
        ),
        edges=(
            edge("DEFINES", RI_REF, ROLE, source_ref="Art. 1"),
            edge("HAS", ROLE, OBLIGATION),
            edge("REQUIRES", OBLIGATION, CAPABILITY),
        ),
    )


def test_every_node_and_edge_lands_in_the_native_graph_as_one_logged_group(
    rig: GatewayRig,
) -> None:
    run = Run(rig)

    run.ingest(_seed())

    assert run.native.nodes == {
        ("RegulatoryInstrument", "TESTREG-1.0"): {
            "title": "Test Regulation",
            "source_type": "internal",
            "effective_date": "2026-01-01",
            "version": "1.0",
            "status": "active",
        },
        ("Role", "role-a"): {"name": "Role A"},
        ("Obligation", "obl-a"): {"text": "Do A"},
        ("Capability", "cap-1"): {"name": "Cap One"},
    }
    assert {key: dict(value) for key, value in run.native.edges.items()} == {
        (
            "DEFINES",
            "RegulatoryInstrument",
            "TESTREG-1.0",
            "Role",
            "role-a",
            "DEFINES:TESTREG-1.0->role-a",
        ): {"identity": "DEFINES:TESTREG-1.0->role-a", "source_ref": "Art. 1"},
        ("HAS", "Role", "role-a", "Obligation", "obl-a", "HAS:role-a->obl-a"): {
            "identity": "HAS:role-a->obl-a"
        },
        (
            "REQUIRES",
            "Obligation",
            "obl-a",
            "Capability",
            "cap-1",
            "REQUIRES:obl-a->cap-1",
        ): {"identity": "REQUIRES:obl-a->cap-1"},
    }
    (group,) = (g for g in rig.store.groups if g.graph == NATIVE_GRAPH)
    assert (group.graph, group.audit_event_id) == (NATIVE_GRAPH, run.audit_event_id)
    assert len(group.entries) == 7


def test_nodes_are_submitted_before_edges_in_document_order() -> None:
    primitives = build_native_primitives(_seed())

    assert [primitive.op for primitive in primitives] == ["upsert_node"] * 4 + ["upsert_edge"] * 3
    assert [p.id for p in primitives[:4] if p.op == "upsert_node"] == [
        "TESTREG-1.0",
        "role-a",
        "obl-a",
        "cap-1",
    ]


def test_an_edge_whose_endpoint_is_not_in_the_document_is_dropped_silently() -> None:
    seed = _seed()
    dangling = edge("REQUIRES", OBLIGATION, ("Capability", "never-declared"))
    seed = InternalRegulationSeed(nodes=seed.nodes, edges=(*seed.edges, dangling))

    primitives = build_native_primitives(seed)  # no error

    edges = [p for p in primitives if p.op == "upsert_edge"]
    assert len(edges) == 3
    assert all(edge.target.id != "never-declared" for edge in edges)


def test_the_dropped_edge_is_not_logged_and_the_survivors_are_written(rig: GatewayRig) -> None:
    run = Run(rig)
    seed = _seed()
    dangling = edge("REQUIRES", OBLIGATION, ("Capability", "never-declared"))
    primitives = build_native_primitives(
        InternalRegulationSeed(nodes=seed.nodes, edges=(*seed.edges, dangling))
    )

    rig.gateway.submit_group(
        MutationGroup(graph=NATIVE_GRAPH, audit_event_id=run.audit_event_id, primitives=primitives)
    )

    assert len(run.native.edges) == 3
    assert len(rig.store.entries[NATIVE_GRAPH]) == 7


def test_a_smaller_property_map_on_reingest_keeps_the_other_properties(rig: GatewayRig) -> None:
    run = Run(rig)
    run.ingest(_seed(capability_properties={"name": "Cap One", "description": "kept"}))

    run.ingest(_seed(capability_properties={"name": "Cap One"}))

    assert run.native.nodes[("Capability", "cap-1")] == {"name": "Cap One", "description": "kept"}


def test_a_changed_value_on_reingest_overwrites_and_a_new_key_is_added(rig: GatewayRig) -> None:
    run = Run(rig)
    run.ingest(_seed(capability_properties={"name": "Cap One", "description": "old"}))

    run.ingest(_seed(capability_properties={"name": "Cap One", "description": "new", "type": "t"}))

    assert run.native.nodes[("Capability", "cap-1")] == {
        "name": "Cap One",
        "description": "new",
        "type": "t",
    }


def test_reingesting_the_same_seed_logs_nothing_more(rig: GatewayRig) -> None:
    run = Run(rig)
    run.ingest(_seed())

    run.ingest(_seed())

    assert len(rig.store.entries[NATIVE_GRAPH]) == 7
    assert [g.graph for g in rig.store.groups].count(NATIVE_GRAPH) == 1


def test_two_same_type_edges_between_the_same_nodes_collapse_to_one(rig: GatewayRig) -> None:
    run = Run(rig)
    seed = _seed()
    twin = edge("DEFINES", ("RegulatoryInstrument", "TESTREG-1.0"), ROLE, source_ref="Art. 2")
    seed = InternalRegulationSeed(nodes=seed.nodes, edges=(*seed.edges, twin))

    run.ingest(seed)

    defines = [key for key in run.native.edges if key[0] == "DEFINES"]
    assert len(defines) == 1
    assert run.native.edges[defines[0]]["source_ref"] == "Art. 2"  # the later SET += wins


@pytest.mark.parametrize(
    "properties",
    [
        pytest.param({"name": "C", "id": "overwrites-the-node-id"}, id="id-key"),
        pytest.param({"name": "C", "embedding": "x"}, id="embedding-key"),
        pytest.param({"name": "C", "score": float("nan")}, id="nan-value"),
        pytest.param({"name": "C", "score": float("inf")}, id="infinite-value"),
    ],
)
def test_a_property_the_graph_cannot_store_is_rejected_before_anything_is_submitted(
    rig: GatewayRig, properties: dict[str, str | float]
) -> None:
    run = Run(rig)

    with pytest.raises(InternalSeedError, match="Capability"):
        run.ingest(_seed(capability_properties=properties))

    assert rig.store.groups == []


def test_a_reserved_key_on_an_edge_is_rejected_before_anything_is_submitted(
    rig: GatewayRig,
) -> None:
    run = Run(rig)
    seed = _seed()
    bad = edge("HAS", ROLE, OBLIGATION, identity="forged")
    seed = InternalRegulationSeed(nodes=seed.nodes, edges=(*seed.edges, bad))

    with pytest.raises(InternalSeedError, match="HAS"):
        run.ingest(seed)

    assert rig.store.groups == []
