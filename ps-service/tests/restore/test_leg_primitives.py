"""`restore.leg_primitives`: an artifact's native or baseline leg as gateway primitives."""

from __future__ import annotations

import pytest

from ps_service.export.models import SerializedEdge, SerializedGraph, SerializedNode
from ps_service.graph_gateway.models import NodeRef, UpsertEdge, UpsertNode
from ps_service.restore.errors import ArtifactContentRejectedError
from ps_service.restore.leg_primitives import fold_serialized_graph, leg_primitives


def _node(label: str, node_id: str, **properties: object) -> SerializedNode:
    return SerializedNode(label=label, properties={"id": node_id, **properties})  # pyright: ignore[reportArgumentType]


def _edge(
    kind: str, source: tuple[str, str], target: tuple[str, str], **properties: object
) -> SerializedEdge:
    return SerializedEdge(
        relationship_type=kind,
        source_label=source[0],
        source_id=source[1],
        target_label=target[0],
        target_id=target[1],
        properties=dict(properties),  # pyright: ignore[reportArgumentType]
    )


_ROLE = ("Role", "role_1")
_OBLIGATION = ("Obligation", "ob_1")


def _graph(*nodes: SerializedNode, edges: tuple[SerializedEdge, ...] = ()) -> SerializedGraph:
    return SerializedGraph(nodes=nodes, edges=edges)


def test_nodes_precede_edges_and_the_id_is_not_a_property() -> None:
    graph = _graph(
        _node("Role", "role_1", name="r"),
        _node("Obligation", "ob_1", text="t"),
        edges=(_edge("HAS", _ROLE, _OBLIGATION),),
    )

    assert leg_primitives(graph) == (
        UpsertNode(label="Role", id="role_1", properties={"name": "r"}),
        UpsertNode(label="Obligation", id="ob_1", properties={"text": "t"}),
        UpsertEdge(
            type="HAS",
            identity="HAS:role_1->ob_1",
            source=NodeRef(label="Role", id="role_1"),
            target=NodeRef(label="Obligation", id="ob_1"),
        ),
    )


def test_an_embedding_travels_in_the_embedding_field() -> None:
    graph = _graph(_node("Capability", "cap_1", name="c", embedding=[0.25, 0.5]))

    (primitive,) = leg_primitives(graph)

    assert isinstance(primitive, UpsertNode)
    assert primitive.embedding == (0.25, 0.5)
    assert "embedding" not in primitive.properties


def test_edge_properties_are_kept_and_the_identity_is_the_formula() -> None:
    graph = _graph(
        _node("RegulatoryInstrument", "X-1"),
        _node("Role", "role_1"),
        edges=(_edge("DEFINES", ("RegulatoryInstrument", "X-1"), _ROLE, source_ref="art. 3"),),
    )

    edge = leg_primitives(graph)[-1]

    assert isinstance(edge, UpsertEdge)
    assert edge.identity == "DEFINES:X-1->role_1"
    assert edge.properties == {"source_ref": "art. 3"}


def test_a_dangling_edge_is_dropped() -> None:
    graph = _graph(_node("Role", "role_1"), edges=(_edge("HAS", _ROLE, _OBLIGATION),))

    assert [type(p) for p in leg_primitives(graph)] == [UpsertNode]


def test_a_repeated_edge_folds_to_one_and_the_last_properties_win() -> None:
    graph = _graph(
        _node("Role", "role_1"),
        _node("Obligation", "ob_1"),
        edges=(
            _edge("HAS", _ROLE, _OBLIGATION, note="first"),
            _edge("HAS", _ROLE, _OBLIGATION, note="second"),
        ),
    )

    edges = [p for p in leg_primitives(graph) if isinstance(p, UpsertEdge)]

    assert [e.properties for e in edges] == [{"note": "second"}]


def test_a_repeated_node_id_keeps_the_last_occurrence() -> None:
    graph = _graph(_node("Role", "role_1", name="first"), _node("Role", "role_1", name="second"))

    assert leg_primitives(graph) == (
        UpsertNode(label="Role", id="role_1", properties={"name": "second"}),
    )
    assert len(fold_serialized_graph(graph).nodes) == 1


def test_an_identity_property_equal_to_the_formula_is_dropped() -> None:
    graph = _graph(
        _node("Role", "role_1"),
        _node("Obligation", "ob_1"),
        edges=(_edge("HAS", _ROLE, _OBLIGATION, identity="HAS:role_1->ob_1"),),
    )

    edge = leg_primitives(graph)[-1]

    assert isinstance(edge, UpsertEdge)
    assert edge.properties == {}


@pytest.mark.parametrize(
    "graph",
    [
        _graph(
            _node("Role", "role_1"),
            _node("Obligation", "ob_1"),
            edges=(_edge("HAS", _ROLE, _OBLIGATION, identity="something-else"),),
        ),
        _graph(_node("Role", "role_1", tags=[])),
        _graph(_node("Role", "role_1", score=float("nan"))),
        _graph(_node("Capability", "cap_1", embedding=[])),
        _graph(
            _node("Role", "role_1"),
            _node("Obligation", "ob_1"),
            edges=(_edge("HAS", _ROLE, _OBLIGATION, embedding=[0.5]),),
        ),
    ],
    ids=["identity-differs", "empty-list", "non-finite", "empty-embedding", "edge-embedding"],
)
def test_a_value_the_gateway_cannot_store_is_a_rejected_artifact(graph: SerializedGraph) -> None:
    with pytest.raises(ArtifactContentRejectedError):
        leg_primitives(graph)


def test_an_empty_graph_has_no_primitives() -> None:
    assert leg_primitives(_graph()) == ()
