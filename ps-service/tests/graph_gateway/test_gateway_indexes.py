"""`id` index management through the public gateway (issue #206, S9; AC-BI-008 index half).

Each apply pass lists the graph's indexes once and creates the missing `id` indexes of the labels
it is about to write, before the first load. Nothing is cached between passes, so a flushed or
swapped-in graph heals itself.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from graph_gateway._fakes import GatewayRig
from ps_service.graph_gateway.models import (
    MutationGroup,
    NodeRef,
    UpsertEdge,
    UpsertNode,
)

if TYPE_CHECKING:
    from graph_gateway._fakes import InMemoryGraph, RecordedQuery
    from ps_service.graph_gateway.models import Primitive

_AUDIT_EVENT_ID = "3f2b8c1e-5d4a-4b7e-9a61-0c2d7e8f9a10"
_GRAPH = "compliance"


def _group(*primitives: Primitive) -> MutationGroup:
    return MutationGroup(graph=_GRAPH, audit_event_id=_AUDIT_EVENT_ID, primitives=primitives)


def _node(label: str, node_id: str, **properties: object) -> UpsertNode:
    return UpsertNode(label=label, id=node_id, properties=properties)


def _created(graph: InMemoryGraph) -> list[str]:
    return [q.labels[0] for q in graph.queries if q.template == "create_index"]


def _listings(graph: InMemoryGraph) -> list[RecordedQuery]:
    return [q for q in graph.queries if q.template == "list_indexes"]


def test_first_write_creates_id_index_per_label_before_the_first_node_load() -> None:
    rig = GatewayRig()

    rig.gateway.submit_group(_group(_node("Capability", "c1"), _node("Policy", "p1")))

    graph = rig.graphs.open(_GRAPH)
    order = [(q.template, q.labels[:1]) for q in graph.queries if q.kind != "read"]
    assert order == [
        ("list_indexes", ()),
        ("create_index", ("Capability",)),
        ("create_index", ("Policy",)),
        ("upsert_node", ("Capability",)),
        ("upsert_node", ("Policy",)),
    ]
    assert graph.indexes == {"Capability", "Policy"}


def test_second_write_does_not_reissue_index_creation() -> None:
    rig = GatewayRig()
    rig.gateway.submit_group(_group(_node("Capability", "c1")))

    rig.gateway.submit_group(_group(_node("Capability", "c2")))

    graph = rig.graphs.open(_GRAPH)
    assert _created(graph) == ["Capability"]
    assert len(_listings(graph)) == 2  # one listing per apply pass, nothing cached


def test_existing_index_is_not_recreated() -> None:
    rig = GatewayRig()
    rig.graphs.open(_GRAPH).indexes.add("Capability")

    rig.gateway.submit_group(_group(_node("Capability", "c1")))

    assert _created(rig.graphs.open(_GRAPH)) == []


def test_edge_endpoint_labels_get_id_indexes() -> None:
    rig = GatewayRig()
    graph = rig.graphs.open(_GRAPH)
    graph.nodes[("Policy", "p1")] = {}
    graph.nodes[("Standard", "s1")] = {}

    rig.gateway.submit_group(
        _group(
            UpsertEdge(
                type="HAS",
                identity="e1",
                source=NodeRef(label="Policy", id="p1"),
                target=NodeRef(label="Standard", id="s1"),
            )
        )
    )

    assert graph.indexes == {"Policy", "Standard"}
    assert _created(graph) == ["Policy", "Standard"]


def test_flushed_graph_gets_index_recreated() -> None:
    rig = GatewayRig()
    rig.gateway.submit_group(_group(_node("Capability", "c1")))
    graph = rig.graphs.open(_GRAPH)
    graph.flush()

    rig.gateway.submit_group(_group(_node("Capability", "c1")))

    assert _created(graph) == ["Capability", "Capability"]
    assert graph.indexes == {"Capability"}
    assert ("Capability", "c1") in graph.nodes


def test_an_index_created_by_a_racing_writer_counts_as_success() -> None:
    rig = GatewayRig()
    graph = rig.graphs.open(_GRAPH)
    graph.labels_indexed_by_a_racing_writer.add("Capability")

    outcome = rig.gateway.submit_group(_group(_node("Capability", "c1")))

    assert outcome.status == "applied"
    assert ("Capability", "c1") in graph.nodes
    assert rig.store.read_applied_position(_GRAPH) == 1


def test_a_group_that_changes_nothing_touches_no_index() -> None:
    rig = GatewayRig()
    rig.gateway.submit_group(_group(_node("Capability", "c1")))
    graph = rig.graphs.open(_GRAPH)
    graph.queries.clear()

    outcome = rig.gateway.submit_group(_group(_node("Capability", "c1")))

    assert outcome.status == "unchanged"
    assert [q for q in graph.queries if q.kind == "index"] == []
