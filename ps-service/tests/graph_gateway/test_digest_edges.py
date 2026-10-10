"""Edges, their direction and parallel edges are part of the digest (#207 S3, AC-RD-002)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from graph_gateway._fakes import GatewayRig, InMemoryGraph
from ps_service.graph_gateway.digest import DigestSettings, canonical_digest
from ps_service.graph_gateway.models import (
    DeleteEdge,
    MutationGroup,
    NodeRef,
    Primitive,
    UpsertEdge,
    UpsertNode,
)

if TYPE_CHECKING:
    from ps_service.ingestion.falkordb_client import GraphQueryResult

_AUDIT_EVENT_ID = "3f2b8c1e-5d4a-4b7e-9a61-0c2d7e8f9a10"
_GRAPH = "g"
_NODES_ONLY_DIGEST = "sha256:3c5c7617dca6844d2a4dc34250c5ea6af6ac957c4c7c4bc3f15c6f7e94946b1c"
"""Pinned digest format v1 of two nodes and no edge: a change of encoding must bump the domain.

(Before labels joined the node element in S5 the value was sha256:53c609ed..., and S3 proved a
graph without edges digests the same as before edge support existed.)
"""


def _submit(rig: GatewayRig, *primitives: Primitive) -> None:
    rig.gateway.submit_group(
        MutationGroup(graph=_GRAPH, audit_event_id=_AUDIT_EVENT_ID, primitives=primitives)
    )


def _nodes() -> tuple[UpsertNode, UpsertNode]:
    return (
        UpsertNode(label="Policy", id="p-1", properties={"name": "a"}),
        UpsertNode(label="Standard", id="s-1", properties={"name": "b"}),
    )


def _edge(
    identity: str = "e-1",
    *,
    kind: str = "HAS",
    reverse: bool = False,
    **properties: object,
) -> UpsertEdge:
    policy, standard = NodeRef(label="Policy", id="p-1"), NodeRef(label="Standard", id="s-1")
    source, target = (standard, policy) if reverse else (policy, standard)
    return UpsertEdge(
        type=kind, identity=identity, source=source, target=target, properties=properties
    )


def _digest(rig: GatewayRig) -> str:
    return canonical_digest(rig.graphs.open(_GRAPH))


def _rig(*edges: UpsertEdge) -> GatewayRig:
    rig = GatewayRig()
    _submit(rig, *_nodes())
    if edges:
        _submit(rig, *edges)
    return rig


@dataclass
class _CountingGraph:
    inner: InMemoryGraph
    answered: list[int] = field(default_factory=list)

    def query(self, q: str, params: dict[str, object] | None = None) -> GraphQueryResult:
        result = self.inner.query(q, params)
        self.answered.append(len(result.result_set))
        return result


def test_node_only_graph_digest_is_pinned_and_edge_support_adds_nothing_to_it() -> None:
    assert _digest(_rig()) == _NODES_ONLY_DIGEST


def test_digest_differs_when_an_edge_is_added_or_removed() -> None:
    without, with_edge = _rig(), _rig(_edge())

    assert _digest(with_edge) != _digest(without)
    _submit(
        with_edge,
        DeleteEdge(
            type="HAS",
            identity="e-1",
            source=NodeRef(label="Policy", id="p-1"),
            target=NodeRef(label="Standard", id="s-1"),
        ),
    )
    assert _digest(with_edge) == _digest(without)


def test_digest_differs_when_relationship_direction_is_reversed() -> None:
    assert _digest(_rig(_edge())) != _digest(_rig(_edge(reverse=True)))


def test_digest_differs_when_edge_type_differs() -> None:
    assert _digest(_rig(_edge(kind="HAS"))) != _digest(_rig(_edge(kind="OWNS")))


def test_digest_differs_when_an_edge_property_differs() -> None:
    assert _digest(_rig(_edge(note="a"))) != _digest(_rig(_edge(note="b")))


def test_parallel_edges_with_different_identities_both_count() -> None:
    one, two = _rig(_edge("e-1")), _rig(_edge("e-1"), _edge("e-2"))
    renamed = _rig(_edge("e-1"), _edge("e-3"))

    assert _digest(two) != _digest(one)
    assert _digest(two) != _digest(renamed)


def test_edges_added_in_a_different_order_give_equal_digests() -> None:
    assert _digest(_rig(_edge("e-1"), _edge("e-2"))) == _digest(_rig(_edge("e-2"), _edge("e-1")))


def test_edge_scan_is_chunked_and_chunk_size_does_not_change_the_digest() -> None:
    rig = _rig(*(_edge(f"e-{i:02d}") for i in range(11)))
    graph = rig.graphs.open(_GRAPH)
    counting = _CountingGraph(graph)

    chunked = canonical_digest(counting, DigestSettings(edge_chunk_rows=4))

    assert chunked == canonical_digest(graph, DigestSettings(edge_chunk_rows=5000))
    assert max(counting.answered[1:]) <= 4  # the first answer is the node scan
    assert counting.answered[1:] == [4, 4, 3]
