"""State reads give back stored 64-bit floats bit-identically (#207 S2L baseline fix, AC-RD-007).

FalkorDB prints a double with 15 significant digits in every reply; the fake graph does the same.
Without the exact-float columns the no-op filter would compare a lossy read against the exact
value to write, and a repeat of a group would be applied again instead of reported `unchanged`.
"""

from __future__ import annotations

import math
from typing import cast

from graph_gateway._fakes import GatewayRig
from ps_service.graph_gateway.graph_reader import read_edges, read_nodes
from ps_service.graph_gateway.models import (
    MutationGroup,
    NodeRef,
    Primitive,
    UpsertEdge,
    UpsertNode,
)

_AUDIT_EVENT_ID = "3f2b8c1e-5d4a-4b7e-9a61-0c2d7e8f9a10"
_GRAPH = "compliance"
_EMBEDDING = (0.1, -0.0, 1.7976931348623157e308, 5e-324, 0.30000000000000004, 1 / 3)


def _submit(rig: GatewayRig, *primitives: Primitive) -> str:
    return rig.gateway.submit_group(
        MutationGroup(graph=_GRAPH, audit_event_id=_AUDIT_EVENT_ID, primitives=primitives)
    ).status


def _node(embedding: tuple[float, ...] = _EMBEDDING, weight: float = 1 / 3) -> UpsertNode:
    return UpsertNode(
        label="Capability",
        id="cap-1",
        properties={"weight": weight, "scores": [0.1, 0.30000000000000004], "n": 1},
        embedding=embedding,
    )


def test_a_node_read_returns_stored_floats_bit_identically() -> None:
    rig = GatewayRig()
    _submit(rig, _node())

    found = read_nodes(rig.graphs.open(_GRAPH), [("Capability", "cap-1")], 500)

    properties = found[("Capability", "cap-1")]
    assert [v.hex() for v in cast("list[float]", properties["embedding"])] == [
        v.hex() for v in _EMBEDDING
    ]
    assert cast("float", properties["weight"]).hex() == (1 / 3).hex()
    assert [v.hex() for v in cast("list[float]", properties["scores"])] == [
        v.hex() for v in (0.1, 0.30000000000000004)
    ]
    assert properties["n"] == 1
    assert not any(math.isinf(v) for v in cast("list[float]", properties["embedding"]))


def test_an_edge_read_returns_stored_floats_bit_identically() -> None:
    rig = GatewayRig()
    _submit(rig, UpsertNode(label="Policy", id="p-1"), UpsertNode(label="Standard", id="s-1"))
    edge = UpsertEdge(
        type="HAS",
        identity="e-1",
        source=NodeRef(label="Policy", id="p-1"),
        target=NodeRef(label="Standard", id="s-1"),
        properties={"weight": 0.30000000000000004, "marks": [1 / 3, 1.7976931348623157e308]},
    )
    _submit(rig, edge)

    found = read_edges(
        rig.graphs.open(_GRAPH), [("HAS", "Policy", "p-1", "Standard", "s-1", "e-1")], 500
    )

    properties = next(iter(found.values()))
    assert cast("float", properties["weight"]).hex() == (0.30000000000000004).hex()
    assert [v.hex() for v in cast("list[float]", properties["marks"])] == [
        (1 / 3).hex(),
        (1.7976931348623157e308).hex(),
    ]


def test_repeating_a_group_with_hard_floats_is_unchanged() -> None:
    rig = GatewayRig()
    assert _submit(rig, _node()) == "applied"

    assert _submit(rig, _node()) == "unchanged"


def test_a_one_ulp_change_of_an_embedding_value_is_still_applied() -> None:
    rig = GatewayRig()
    _submit(rig, _node())
    nudged = (*_EMBEDDING[:-1], math.nextafter(_EMBEDDING[-1], 1.0))

    assert _submit(rig, _node(embedding=nudged)) == "applied"
