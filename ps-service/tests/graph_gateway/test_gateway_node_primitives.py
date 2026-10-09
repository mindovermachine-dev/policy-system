"""Property and delete-node primitives through the Graph Write Gateway (issue #206 S4, AC-BI-003).

Drives the public `submit_group` with `MergeProperty`, `RemoveProperty` and `DeleteNode` against
the Protocol fakes; the Cypher builder is also hit directly where the hostile-key defence has a
second layer (F5).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from graph_gateway._fakes import GatewayRig
from ps_service.graph_gateway import cypher
from ps_service.graph_gateway.entry_codec import decode_entry
from ps_service.graph_gateway.errors import MissingTargetError, UnlistedNameError
from ps_service.graph_gateway.models import (
    DeleteNode,
    MergeProperty,
    MutationGroup,
    NodeRef,
    Primitive,
    RemoveProperty,
    UpsertEdge,
    UpsertNode,
)

_AUDIT_EVENT_ID = "3f2b8c1e-5d4a-4b7e-9a61-0c2d7e8f9a10"
_GRAPH = "compliance"
_CAP = ("Capability", "cap-1")


def _submit(rig: GatewayRig, *primitives: Primitive) -> None:
    rig.gateway.submit_group(
        MutationGroup(graph=_GRAPH, audit_event_id=_AUDIT_EVENT_ID, primitives=primitives)
    )


def _seeded_rig() -> GatewayRig:
    rig = GatewayRig()
    _submit(
        rig,
        UpsertNode(
            label="Capability",
            id="cap-1",
            properties={"name": "a", "status": "draft", "owner": "x"},
        ),
    )
    rig.events.clear()
    return rig


def test_merge_property_updates_node_and_is_logged() -> None:
    rig = _seeded_rig()

    _submit(rig, MergeProperty(label="Capability", id="cap-1", properties={"status": "active"}))

    node = rig.graphs.open(_GRAPH).nodes[_CAP]
    assert node == {"name": "a", "status": "active", "owner": "x"}
    entry = rig.store.entries[_GRAPH][-1]
    assert (entry.name, entry.identity) == _CAP
    assert entry.content == {"op": "merge_property", "properties": {"status": "active"}}
    assert rig.store.read_applied_position(_GRAPH) == rig.store.last_position(_GRAPH) == 2


def test_remove_property_removes_key() -> None:
    rig = _seeded_rig()

    _submit(rig, RemoveProperty(label="Capability", id="cap-1", keys=("status", "owner")))

    assert rig.graphs.open(_GRAPH).nodes[_CAP] == {"name": "a"}
    entry = rig.store.entries[_GRAPH][-1]
    assert entry.content == {"op": "remove_property", "keys": ["status", "owner"]}
    assert rig.store.read_applied_position(_GRAPH) == 2


def test_delete_node_detaches_and_removes_node() -> None:
    rig = GatewayRig()
    edge = UpsertEdge(
        type="HAS",
        identity="e-1",
        source=NodeRef(label="Policy", id="p-1"),
        target=NodeRef(label="Standard", id="s-1"),
    )
    _submit(
        rig,
        UpsertNode(label="Policy", id="p-1"),
        UpsertNode(label="Standard", id="s-1"),
        edge,
    )
    graph = rig.graphs.open(_GRAPH)
    assert len(graph.edges) == 1

    _submit(rig, DeleteNode(label="Standard", id="s-1"))

    assert set(graph.nodes) == {("Policy", "p-1")}
    assert graph.edges == {}
    assert rig.store.entries[_GRAPH][-1].content == {"op": "delete_node"}
    assert rig.store.read_applied_position(_GRAPH) == rig.store.last_position(_GRAPH) == 4


def test_new_primitives_decode_back_to_what_was_submitted() -> None:
    rig = _seeded_rig()
    submitted: tuple[Primitive, ...] = (
        MergeProperty(label="Capability", id="cap-1", properties={"status": "active", "n": 2}),
        RemoveProperty(label="Capability", id="cap-1", keys=("owner",)),
        DeleteNode(label="Capability", id="cap-1"),
    )

    _submit(rig, *submitted)

    recorded = rig.store.entries[_GRAPH][1:]
    assert tuple(decode_entry(entry) for entry in recorded) == submitted


def test_hostile_property_key_rejected_at_boundary_and_at_builder() -> None:
    hostile = "x) DETACH DELETE n //"

    with pytest.raises(ValidationError):
        RemoveProperty(label="Capability", id="cap-1", keys=(hostile,))
    with pytest.raises(ValueError, match="safe Cypher identifier"):
        cypher.remove_property_query("Capability", (hostile,))


@pytest.mark.parametrize("keys", [(), ("id",), ("embedding",), ("a", "a"), ("",), ("1abc",)])
def test_remove_property_rejects_empty_reserved_duplicate_or_malformed_keys(
    keys: tuple[str, ...],
) -> None:
    with pytest.raises(ValidationError):
        RemoveProperty(label="Capability", id="cap-1", keys=keys)


def test_builder_refuses_to_remove_a_reserved_key() -> None:
    with pytest.raises(ValueError, match="reserved"):
        cypher.remove_property_query("Capability", ("id",))


def test_merge_property_requires_properties_and_rejects_reserved_keys() -> None:
    with pytest.raises(ValidationError):
        MergeProperty(label="Capability", id="cap-1", properties={})
    with pytest.raises(ValidationError):
        MergeProperty(label="Capability", id="cap-1", properties={"id": "other"})


def test_merge_property_on_absent_node_rejected() -> None:
    rig = _seeded_rig()

    with pytest.raises(MissingTargetError):
        _submit(rig, MergeProperty(label="Capability", id="ghost", properties={"a": 1}))

    assert rig.store.last_position(_GRAPH) == 1
    assert rig.graphs.open(_GRAPH).nodes[_CAP]["status"] == "draft"


def test_merge_property_on_node_created_earlier_in_same_group_is_accepted() -> None:
    rig = GatewayRig()

    _submit(
        rig,
        UpsertNode(label="Capability", id="cap-1"),
        MergeProperty(label="Capability", id="cap-1", properties={"status": "active"}),
    )

    assert rig.graphs.open(_GRAPH).nodes[_CAP] == {"status": "active"}


def test_merge_property_on_node_deleted_earlier_in_same_group_is_rejected() -> None:
    rig = _seeded_rig()

    with pytest.raises(MissingTargetError):
        _submit(
            rig,
            DeleteNode(label="Capability", id="cap-1"),
            MergeProperty(label="Capability", id="cap-1", properties={"a": 1}),
        )

    assert rig.store.last_position(_GRAPH) == 1


def test_edge_to_node_deleted_earlier_in_same_group_is_rejected() -> None:
    rig = GatewayRig()
    _submit(rig, UpsertNode(label="Policy", id="p-1"), UpsertNode(label="Standard", id="s-1"))
    edge = UpsertEdge(
        type="HAS",
        identity="e-1",
        source=NodeRef(label="Policy", id="p-1"),
        target=NodeRef(label="Standard", id="s-1"),
    )

    with pytest.raises(MissingTargetError):
        _submit(rig, DeleteNode(label="Standard", id="s-1"), edge)

    assert rig.store.last_position(_GRAPH) == 2


def test_unlisted_label_on_a_new_primitive_is_rejected_before_logging() -> None:
    rig = GatewayRig()

    for primitive in (
        MergeProperty(label="NotInTheSchema", id="x", properties={"a": 1}),
        RemoveProperty(label="NotInTheSchema", id="x", keys=("a",)),
        DeleteNode(label="NotInTheSchema", id="x"),
    ):
        with pytest.raises(UnlistedNameError):
            _submit(rig, primitive)

    assert rig.store.entries == {}
    assert rig.events == []
