"""`StampEdgeIdentity` through the Graph Write Gateway (issue #208 S8, F1).

A graph written before the gateway holds edges with no `identity`. The primitive gives such an
edge its identity in place, so a later `UpsertEdge` merges into it instead of creating a parallel
edge. Every test enters at `submit_group` / `replay_graph` against the Protocol fakes.
"""

from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError

from graph_gateway._fakes import GatewayRig
from ps_service.graph_gateway.digest import canonical_digest
from ps_service.graph_gateway.entry_codec import decode_entry, encode_primitive
from ps_service.graph_gateway.errors import UnlistedNameError
from ps_service.graph_gateway.models import (
    GraphLogEntry,
    MutationGroup,
    NodeRef,
    Primitive,
    StampEdgeIdentity,
    UpsertEdge,
    UpsertNode,
)

_AUDIT_EVENT_ID = "3f2b8c1e-5d4a-4b7e-9a61-0c2d7e8f9a10"
_GRAPH = "compliance"
_POLICY = NodeRef(label="Policy", id="p-1")
_STANDARD = NodeRef(label="Standard", id="s-1")
_IDENTITY = "HAS:p-1->s-1"
_STAMPED_KEY = ("HAS", "Policy", "p-1", "Standard", "s-1", _IDENTITY)
_LEGACY_KEY = ("HAS", "Policy", "p-1", "Standard", "s-1", "")


def _stamp(identity: str = "HAS:p-1->s-1", *, rel: str = "HAS") -> StampEdgeIdentity:
    return StampEdgeIdentity(type=rel, identity=identity, source=_POLICY, target=_STANDARD)


def _submit(rig: GatewayRig, *primitives: Primitive) -> str:
    return rig.gateway.submit_group(
        MutationGroup(graph=_GRAPH, audit_event_id=_AUDIT_EVENT_ID, primitives=primitives)
    ).status


def _legacy_rig(**properties: object) -> GatewayRig:
    """Both endpoints logged through the gateway; the one edge between them is legacy-shaped."""
    rig = GatewayRig()
    _submit(rig, UpsertNode(label="Policy", id="p-1"), UpsertNode(label="Standard", id="s-1"))
    rig.graphs.open(_GRAPH).seed_legacy_edge(_LEGACY_KEY[:-1], properties)
    rig.graphs.open(_GRAPH).queries.clear()
    rig.events.clear()
    return rig


def test_stamp_requires_a_non_blank_identity_and_a_safe_type() -> None:
    with pytest.raises(ValidationError):
        StampEdgeIdentity(type="HAS", identity="", source=_POLICY, target=_STANDARD)
    with pytest.raises(ValidationError):
        StampEdgeIdentity(type="HAS) DELETE", identity="x", source=_POLICY, target=_STANDARD)


def test_stamp_round_trips_through_the_log_entry_codec() -> None:
    stamp = _stamp()

    draft = encode_primitive(stamp)
    entry = GraphLogEntry(graph=_GRAPH, position=1, group_id=uuid.uuid4(), **draft.model_dump())

    assert draft.content["op"] == "stamp_edge_identity"
    assert (draft.name, draft.identity) == ("HAS", "HAS:p-1->s-1")
    assert decode_entry(entry) == stamp


def test_stamp_gives_the_legacy_edge_its_identity_in_place_keeping_its_properties() -> None:
    rig = _legacy_rig(weight=2)
    graph = rig.graphs.open(_GRAPH)

    status = _submit(rig, _stamp())

    assert status == "applied"
    assert list(graph.edges.items()) == [
        (
            ("HAS", "Policy", "p-1", "Standard", "s-1", "HAS:p-1->s-1"),
            {"weight": 2, "identity": "HAS:p-1->s-1"},
        )
    ]
    assert rig.store.entries[_GRAPH][-1].content["op"] == "stamp_edge_identity"
    assert rig.store.read_applied_position(_GRAPH) == rig.store.last_position(_GRAPH)


def test_second_stamp_is_unchanged_and_logs_and_writes_nothing() -> None:
    rig = _legacy_rig()
    _submit(rig, _stamp())
    logged = rig.store.last_position(_GRAPH)
    graph = rig.graphs.open(_GRAPH)
    graph.queries.clear()

    status = _submit(rig, _stamp())

    assert status == "unchanged"
    assert rig.store.last_position(_GRAPH) == logged
    assert [q for q in graph.queries if q.kind == "write"] == []


def test_stamp_of_an_edge_that_does_not_exist_is_a_noop() -> None:
    rig = GatewayRig()
    _submit(rig, UpsertNode(label="Policy", id="p-1"), UpsertNode(label="Standard", id="s-1"))

    assert _submit(rig, _stamp()) == "unchanged"
    assert rig.graphs.open(_GRAPH).edges == {}


def test_upsert_after_a_stamp_merges_into_the_stamped_edge_without_a_parallel_edge() -> None:
    rig = _legacy_rig(weight=2)

    _submit(
        rig,
        _stamp(),
        UpsertEdge(
            type="HAS",
            identity="HAS:p-1->s-1",
            source=_POLICY,
            target=_STANDARD,
            properties={"weight": 3},
        ),
    )

    assert list(rig.graphs.open(_GRAPH).edges.values()) == [
        {"weight": 3, "identity": "HAS:p-1->s-1"}
    ]


def test_upsert_without_a_stamp_leaves_the_legacy_edge_and_adds_a_parallel_one() -> None:
    """Why the primitive exists: the MERGE on `identity` cannot see an identity-less edge."""
    rig = _legacy_rig()

    _submit(
        rig,
        UpsertEdge(type="HAS", identity="HAS:p-1->s-1", source=_POLICY, target=_STANDARD),
    )

    assert len(rig.graphs.open(_GRAPH).edges) == 2


def test_stamp_with_an_unlisted_type_or_label_is_rejected_before_anything_is_logged() -> None:
    rig = _legacy_rig()
    before = rig.store.last_position(_GRAPH)

    with pytest.raises(UnlistedNameError):
        _submit(rig, _stamp(rel="NOT_A_TYPE"))
    with pytest.raises(UnlistedNameError):
        _submit(
            rig,
            StampEdgeIdentity(
                type="HAS",
                identity="x",
                source=NodeRef(label="NotALabel", id="p-1"),
                target=_STANDARD,
            ),
        )

    assert rig.store.last_position(_GRAPH) == before


def test_replay_of_stamp_then_reingest_rebuilds_the_same_digest_as_the_live_graph() -> None:
    """A re-ingest that changes the edge is logged, so the log rebuilds it; the stamp replays
    as a no-op on a graph that never held the legacy edge (legacy data predates the log).
    """
    rig = _legacy_rig(weight=2)
    _submit(
        rig,
        _stamp(),
        UpsertEdge(
            type="HAS",
            identity="HAS:p-1->s-1",
            source=_POLICY,
            target=_STANDARD,
            properties={"weight": 3},
        ),
    )
    live = canonical_digest(rig.graphs.open(_GRAPH))
    rig.graphs.open(_GRAPH).flush()

    rig.restart().replay_graph(_GRAPH)

    assert canonical_digest(rig.graphs.open(_GRAPH)) == live
