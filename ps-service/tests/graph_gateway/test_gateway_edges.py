"""Edge primitives through the Graph Write Gateway (issue #206 S3: AC-BI-003, AC-BI-001).

Drives the public `submit_group` with `UpsertEdge` / `DeleteEdge` against the Protocol fakes.
"""

from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError

from graph_gateway._fakes import GatewayRig
from ps_service.graph_gateway.entry_codec import decode_entry
from ps_service.graph_gateway.errors import (
    GraphLogEntryDecodeError,
    MissingTargetError,
    UnlistedNameError,
)
from ps_service.graph_gateway.models import (
    DeleteEdge,
    GraphLogEntry,
    MutationGroup,
    NodeRef,
    Primitive,
    UpsertEdge,
    UpsertNode,
)

_AUDIT_EVENT_ID = "3f2b8c1e-5d4a-4b7e-9a61-0c2d7e8f9a10"
_GRAPH = "compliance"


def _node(node_id: str, label: str = "Policy") -> UpsertNode:
    return UpsertNode(label=label, id=node_id)


def _edge(
    identity: str, *, source: str = "p-1", target: str = "s-1", rel: str = "HAS", **props: object
) -> UpsertEdge:
    return UpsertEdge(
        type=rel,
        identity=identity,
        source=NodeRef(label="Policy", id=source),
        target=NodeRef(label="Standard", id=target),
        properties=props,
    )


def _delete(identity: str, *, source: str = "p-1", target: str = "s-1") -> DeleteEdge:
    return DeleteEdge(
        type="HAS",
        identity=identity,
        source=NodeRef(label="Policy", id=source),
        target=NodeRef(label="Standard", id=target),
    )


def _submit(rig: GatewayRig, *primitives: Primitive) -> None:
    rig.gateway.submit_group(
        MutationGroup(graph=_GRAPH, audit_event_id=_AUDIT_EVENT_ID, primitives=primitives)
    )


def _seeded_rig() -> GatewayRig:
    rig = GatewayRig()
    _submit(rig, _node("p-1"), _node("s-1", "Standard"))
    rig.events.clear()
    return rig


def test_upsert_edge_group_is_logged_then_applied_between_existing_nodes() -> None:
    rig = _seeded_rig()

    _submit(rig, _edge("e-1", weight=2))

    assert rig.events == [
        "graph_read",  # Policy nodes
        "graph_read",  # Standard nodes
        "graph_read",  # the edge itself
        "log_append",
        "graph_write",
    ]
    (_, _, edge_entry) = rig.store.entries[_GRAPH]
    assert (edge_entry.name, edge_entry.identity) == ("HAS", "e-1")
    assert edge_entry.content == {
        "op": "upsert_edge",
        "source": {"label": "Policy", "id": "p-1"},
        "target": {"label": "Standard", "id": "s-1"},
        "properties": {"weight": 2},
    }
    edges = rig.graphs.open(_GRAPH).edges
    assert list(edges.values()) == [{"identity": "e-1", "weight": 2}]
    assert rig.store.read_applied_position(_GRAPH) == 3


def test_delete_edge_group_removes_edge_and_is_logged() -> None:
    rig = _seeded_rig()
    _submit(rig, _edge("e-1"))

    _submit(rig, _delete("e-1"))

    assert rig.graphs.open(_GRAPH).edges == {}
    assert rig.store.entries[_GRAPH][-1].content["op"] == "delete_edge"
    assert rig.store.read_applied_position(_GRAPH) == rig.store.last_position(_GRAPH) == 4


def test_upsert_edge_with_absent_endpoint_rejected_nothing_logged() -> None:
    rig = _seeded_rig()

    with pytest.raises(MissingTargetError):
        _submit(rig, _edge("e-1", target="missing"))

    assert rig.store.last_position(_GRAPH) == 2
    assert rig.graphs.open(_GRAPH).edges == {}
    assert set(rig.events) == {"graph_read"}  # state was read, nothing logged or written


def test_edge_endpoint_created_earlier_in_same_group_is_accepted() -> None:
    rig = GatewayRig()

    _submit(rig, _node("p-1"), _node("s-1", "Standard"), _edge("e-1"))

    assert rig.events == [
        "graph_read",
        "graph_read",
        "graph_read",
        "log_append",
        "graph_write",
        "graph_write",
        "graph_write",
    ]
    assert len(rig.graphs.open(_GRAPH).edges) == 1


def test_edge_endpoint_created_only_later_in_the_group_is_rejected() -> None:
    rig = GatewayRig()

    with pytest.raises(MissingTargetError):
        _submit(rig, _edge("e-1"), _node("p-1"), _node("s-1", "Standard"))

    assert rig.store.entries == {}


def test_parallel_edges_distinct_identity_delete_only_named_edge() -> None:
    rig = _seeded_rig()
    _submit(rig, _edge("e-1"), _edge("e-2"))
    assert len(rig.graphs.open(_GRAPH).edges) == 2

    _submit(rig, _delete("e-1"))

    remaining = rig.graphs.open(_GRAPH).edges
    assert [edge["identity"] for edge in remaining.values()] == ["e-2"]


def test_upserting_the_same_edge_identity_twice_keeps_one_edge() -> None:
    rig = _seeded_rig()

    _submit(rig, _edge("e-1", weight=1))
    _submit(rig, _edge("e-1", weight=2))

    assert [edge["weight"] for edge in rig.graphs.open(_GRAPH).edges.values()] == [2]


def test_unlisted_relationship_type_rejected() -> None:
    rig = _seeded_rig()

    with pytest.raises(UnlistedNameError):
        _submit(rig, _edge("e-1", rel="SECRET_LINK"))

    assert rig.store.last_position(_GRAPH) == 2


def test_unlisted_edge_endpoint_label_rejected() -> None:
    rig = _seeded_rig()
    edge = UpsertEdge(
        type="HAS",
        identity="e-1",
        source=NodeRef(label="Policy", id="p-1"),
        target=NodeRef(label="NotInTheSchema", id="x"),
    )

    with pytest.raises(UnlistedNameError):
        _submit(rig, edge)

    assert rig.store.last_position(_GRAPH) == 2
    assert rig.events == []


def test_delete_edge_with_unlisted_type_is_rejected() -> None:
    rig = _seeded_rig()
    delete = DeleteEdge(
        type="SECRET_LINK",
        identity="e-1",
        source=NodeRef(label="Policy", id="p-1"),
        target=NodeRef(label="Standard", id="s-1"),
    )

    with pytest.raises(UnlistedNameError):
        _submit(rig, delete)


_ENDPOINTS: dict[str, object] = {
    "source": {"label": "Policy", "id": "a"},
    "target": {"label": "Standard", "id": "b"},
}


@pytest.mark.parametrize(
    "fields",
    [
        {"type": "HAS", **_ENDPOINTS},
        {"type": "HAS", "identity": " ", **_ENDPOINTS},
        {"type": "HAS", "identity": "e", "properties": {"identity": "other"}, **_ENDPOINTS},
    ],
)
def test_edge_without_caller_identity_or_with_reserved_key_is_rejected(
    fields: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        UpsertEdge.model_validate(fields)


def test_unknown_log_operation_does_not_decode_and_names_position_only() -> None:
    entry = GraphLogEntry(
        graph=_GRAPH,
        position=7,
        group_id=uuid.UUID("3f2b8c1e-5d4a-4b7e-9a61-0c2d7e8f9a11"),
        name="Policy",
        identity="p-1",
        content={"op": "teleport", "secret": "sentinel"},
    )

    with pytest.raises(GraphLogEntryDecodeError) as raised:
        decode_entry(entry)

    assert "7" in str(raised.value)
    assert "sentinel" not in str(raised.value)
