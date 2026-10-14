"""`GraphWriteGateway.read_persisted_elements` enumerates a graph's ids and edge identities.

Read-only: the node ids and edge identities the graph holds, the edges that carry no identity,
and the log position the read stands at. The reply shape is checked, so a row that cannot be
classified fails closed.
"""

from __future__ import annotations

import pytest

from graph_gateway._fakes import GatewayRig
from ps_service.graph_gateway.cypher import REPLAY_STATE_LABEL
from ps_service.graph_gateway.digest import DigestSettings
from ps_service.graph_gateway.errors import UnexpectedGraphReplyError
from ps_service.graph_gateway.graph_reader import read_persisted_elements
from ps_service.graph_gateway.models import MutationGroup, NodeRef, UpsertEdge, UpsertNode

_AUDIT_EVENT_ID = "3f2b8c1e-5d4a-4b7e-9a61-0c2d7e8f9a10"
_GRAPH = "acme_baseline"


@pytest.fixture
def rig() -> GatewayRig:
    """A gateway over in-memory log and graph fakes (closed by the package's autouse fixture)."""
    return GatewayRig()


def _seed(rig: GatewayRig, node_count: int = 3) -> None:
    nodes = tuple(UpsertNode(label="Standard", id=f"s-{index}") for index in range(node_count))
    edge = UpsertEdge(
        type="SUPPORTED_BY",
        identity="SUPPORTED_BY:p-1->s-0",
        source=NodeRef(label="Policy", id="p-1"),
        target=NodeRef(label="Standard", id="s-0"),
    )
    rig.gateway.submit_group(
        MutationGroup(
            graph=_GRAPH,
            audit_event_id=_AUDIT_EVENT_ID,
            primitives=(UpsertNode(label="Policy", id="p-1"), *nodes, edge),
        )
    )


def test_the_read_returns_node_keys_edge_keys_and_the_log_position(rig: GatewayRig) -> None:
    _seed(rig)

    elements = rig.gateway.read_persisted_elements(_GRAPH)

    assert elements.nodes == {
        ("Policy", "p-1"),
        ("Standard", "s-0"),
        ("Standard", "s-1"),
        ("Standard", "s-2"),
    }
    assert elements.edges == {
        ("SUPPORTED_BY", "Policy", "p-1", "Standard", "s-0", "SUPPORTED_BY:p-1->s-0")
    }
    assert elements.legacy_edges == frozenset()
    assert elements.log_position == rig.store.last_position(_GRAPH)


def test_an_edge_without_identity_is_reported_apart_from_the_identified_ones(
    rig: GatewayRig,
) -> None:
    graph = rig.graphs.open(_GRAPH)
    graph.seed_node(("Policy",), "p-1", {})
    graph.seed_node(("Standard",), "s-1", {})
    graph.seed_legacy_edge(("SUPPORTED_BY", "Policy", "p-1", "Standard", "s-1"), {})

    elements = rig.gateway.read_persisted_elements(_GRAPH)

    assert elements.edges == frozenset()
    assert elements.legacy_edges == {("SUPPORTED_BY", "Policy", "p-1", "Standard", "s-1")}


def test_the_replay_sentinel_is_not_an_element(rig: GatewayRig) -> None:
    _seed(rig, node_count=1)
    rig.graphs.open(_GRAPH).seed_node((REPLAY_STATE_LABEL,), "sentinel", {})

    elements = rig.gateway.read_persisted_elements(_GRAPH)

    assert all(label != REPLAY_STATE_LABEL for label, _ in elements.nodes)


def test_the_read_spans_scan_chunks(rig: GatewayRig) -> None:
    _seed(rig, node_count=5)

    elements = read_persisted_elements(
        rig.graphs.open(_GRAPH),
        log_position=0,
        settings=DigestSettings(node_chunk_rows=2, edge_chunk_rows=1),
    )

    assert len(elements.nodes) == 6
    assert len(elements.edges) == 1


def test_the_read_writes_nothing(rig: GatewayRig) -> None:
    _seed(rig)
    graph = rig.graphs.open(_GRAPH)
    logged, queried = len(rig.store.groups), len(graph.queries)

    rig.gateway.read_persisted_elements(_GRAPH)

    assert len(rig.store.groups) == logged
    assert {query.kind for query in graph.queries[queried:]} == {"read"}


def test_a_node_with_several_labels_is_rejected(rig: GatewayRig) -> None:
    rig.graphs.open(_GRAPH).seed_node(("Standard", "Extra"), "s-1", {})

    with pytest.raises(UnexpectedGraphReplyError):
        rig.gateway.read_persisted_elements(_GRAPH)
