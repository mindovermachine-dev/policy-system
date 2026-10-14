"""A replacing leg leaves its graph holding exactly the leg's content.

The gateway expands a leg, under the graph's lock and at the group's position, into ordinary
logged primitives placed before the caller's upserts: a stamp for every identity-less edge, a
delete for every live element the leg does not upsert, and a delete-then-recreate for a survivor
that holds a property the leg's element lacks. Every test enters at `submit_multi_graph_group`
against the Protocol fakes; the log is read back through `decode_entry`.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

import pytest
from pydantic import ValidationError

from graph_gateway._fakes import GatewayRig
from ps_service.graph_gateway.digest import canonical_digest
from ps_service.graph_gateway.entry_codec import decode_entry
from ps_service.graph_gateway.errors import StaleGraphStateError, UnlistedNameError
from ps_service.graph_gateway.models import (
    DeleteNode,
    ExpectedPosition,
    GraphLeg,
    MergeProperty,
    MultiGraphGroup,
    MutationGroup,
    NodeRef,
    UpsertEdge,
    UpsertNode,
)

if TYPE_CHECKING:
    from pathlib import Path
    from typing import Protocol

    from ps_service.graph_gateway.models import MultiGraphOutcome, Primitive
    from ps_service.logging import LogEmitter

    class MakeEmitter(Protocol):
        """Call shape of the shared `make_emitter` fixture (`tests/conftest.py`)."""

        def __call__(self) -> tuple[LogEmitter, Path]: ...

    class ReadLines(Protocol):
        """Call shape of the shared `read_lines` fixture (`tests/conftest.py`)."""

        def __call__(self, log_path: Path) -> list[dict[str, object]]: ...


_AUDIT_EVENT_ID = "3f2b8c1e-5d4a-4b7e-9a61-0c2d7e8f9a10"
_OTHER_EVENT_ID = "9b1e2f3a-4c5d-4e6f-8a7b-1c2d3e4f5a6b"
_NATIVE = "native"
_MERGE = "policy_system"
_WAIT_SECONDS = 5.0
_BLOCKED_PROBE_SECONDS = 0.3


def _policy(node_id: str, **properties: object) -> UpsertNode:
    return UpsertNode(label="Policy", id=node_id, properties=properties)


def _standard(node_id: str, **properties: object) -> UpsertNode:
    return UpsertNode(label="Standard", id=node_id, properties=properties)


def _has(source: str, target: str, **properties: object) -> UpsertEdge:
    return UpsertEdge(
        type="HAS",
        identity=f"HAS:{source}->{target}",
        source=NodeRef(label="Policy", id=source),
        target=NodeRef(label="Standard", id=target),
        properties=properties,
    )


def _edge_key(source: str, target: str) -> tuple[str, str, str, str, str, str]:
    return ("HAS", "Policy", source, "Standard", target, f"HAS:{source}->{target}")


def _restore(
    rig: GatewayRig,
    *primitives: Primitive,
    merge: tuple[Primitive, ...] = (UpsertNode(label="Capability", id="m"),),
    audit_event_id: str = _AUDIT_EVENT_ID,
    merge_preconditions: tuple[ExpectedPosition, ...] = (),
) -> MultiGraphOutcome:
    """A restore-shaped set: the replacing `native` leg and a merging `policy_system` leg."""
    return rig.gateway.submit_multi_graph_group(
        MultiGraphGroup(
            audit_event_id=audit_event_id,
            legs=(
                GraphLeg(graph=_NATIVE, primitives=primitives, replace=True),
                GraphLeg(graph=_MERGE, primitives=merge, preconditions=merge_preconditions),
            ),
        )
    )


def _logged(rig: GatewayRig, graph: str = _NATIVE) -> list[str]:
    """The operations a graph's log holds, in position order."""
    return [type(decode_entry(entry)).__name__ for entry in rig.store.entries.get(graph, [])]


def _nodes(rig: GatewayRig) -> dict[tuple[str, str], dict[str, object]]:
    return rig.graphs.open(_NATIVE).nodes


def _edges(rig: GatewayRig) -> dict[tuple[str, str, str, str, str, str], dict[str, object]]:
    return rig.graphs.open(_NATIVE).edges


def _seeded(rig: GatewayRig) -> None:
    """Content a previous restore left behind: p-1 -HAS-> s-1, p-2 -HAS-> s-1."""
    _restore(
        rig,
        _policy("p-1", name="one"),
        _policy("p-2", name="two"),
        _standard("s-1", name="std"),
        _has("p-1", "s-1"),
        _has("p-2", "s-1"),
    )


# -- the model --------------------------------------------------------------------------------


def test_a_replacing_leg_may_hold_only_upserts() -> None:
    for refused in (
        DeleteNode(label="Policy", id="p-1"),
        MergeProperty(label="Policy", id="p-1", properties={"k": 1}),
    ):
        with pytest.raises(ValidationError):
            GraphLeg(graph=_NATIVE, primitives=(_policy("p-1"), refused), replace=True)


# -- over an empty or an equal graph -----------------------------------------------------------


def test_replace_over_an_empty_graph_is_the_plain_upserts() -> None:
    rig = GatewayRig()

    outcome = _restore(rig, _policy("p-1"), _standard("s-1"), _has("p-1", "s-1"))

    assert _logged(rig) == ["UpsertNode", "UpsertNode", "UpsertEdge"]
    assert outcome.legs[0].status == "applied"
    assert set(_nodes(rig)) == {("Policy", "p-1"), ("Standard", "s-1")}


def test_replacing_with_identical_content_is_unchanged_and_logs_nothing() -> None:
    rig = GatewayRig()
    _seeded(rig)
    logged = rig.store.logged_count(_NATIVE)

    outcome = _restore(
        rig,
        _policy("p-1", name="one"),
        _policy("p-2", name="two"),
        _standard("s-1", name="std"),
        _has("p-1", "s-1"),
        _has("p-2", "s-1"),
        audit_event_id=_OTHER_EVENT_ID,
    )

    assert outcome.legs[0].status == "unchanged"
    assert rig.store.logged_count(_NATIVE) == logged


# -- what must go ------------------------------------------------------------------------------


def test_a_live_node_and_edge_the_leg_lacks_are_deleted() -> None:
    rig = GatewayRig()
    _seeded(rig)

    _restore(
        rig,
        _policy("p-1", name="one"),
        _standard("s-1", name="std"),
        _has("p-1", "s-1"),
        audit_event_id=_OTHER_EVENT_ID,
    )

    assert set(_nodes(rig)) == {("Policy", "p-1"), ("Standard", "s-1")}
    assert set(_edges(rig)) == {_edge_key("p-1", "s-1")}
    assert _logged(rig)[5:] == ["DeleteNode"]  # the node's edge goes with it: one entry, not two


def test_a_live_edge_between_surviving_nodes_that_the_leg_lacks_is_deleted() -> None:
    rig = GatewayRig()
    _seeded(rig)

    _restore(
        rig,
        _policy("p-1", name="one"),
        _policy("p-2", name="two"),
        _standard("s-1", name="std"),
        _has("p-1", "s-1"),
        audit_event_id=_OTHER_EVENT_ID,
    )

    assert set(_edges(rig)) == {_edge_key("p-1", "s-1")}
    assert _logged(rig)[5:] == ["DeleteEdge"]


def test_a_changed_value_is_overwritten_without_deleting_anything() -> None:
    rig = GatewayRig()
    _seeded(rig)

    _restore(
        rig,
        _policy("p-1", name="renamed"),
        _policy("p-2", name="two"),
        _standard("s-1", name="std"),
        _has("p-1", "s-1"),
        _has("p-2", "s-1"),
        audit_event_id=_OTHER_EVENT_ID,
    )

    assert _nodes(rig)[("Policy", "p-1")]["name"] == "renamed"
    assert _logged(rig)[5:] == ["UpsertNode"]


# -- survivors with surplus properties -------------------------------------------------------


def test_a_survivor_holding_a_property_the_leg_lacks_is_deleted_and_recreated() -> None:
    rig = GatewayRig()
    _restore(
        rig,
        _policy("p-1", name="one", note="old note"),
        _standard("s-1"),
        _has("p-1", "s-1"),
    )

    _restore(
        rig,
        _policy("p-1", name="one"),
        _standard("s-1"),
        _has("p-1", "s-1"),
        audit_event_id=_OTHER_EVENT_ID,
    )

    assert _nodes(rig)[("Policy", "p-1")] == {"name": "one"}
    assert set(_edges(rig)) == {_edge_key("p-1", "s-1")}  # the artifact's edge came back
    assert _logged(rig)[3:] == ["DeleteNode", "UpsertNode", "UpsertEdge"]


def test_a_survivor_holding_an_embedding_the_leg_lacks_loses_it() -> None:
    rig = GatewayRig()
    _restore(rig, UpsertNode(label="Policy", id="p-1", embedding=(0.5, 0.25)), _standard("s-1"))

    _restore(rig, _policy("p-1"), _standard("s-1"), audit_event_id=_OTHER_EVENT_ID)

    assert "embedding" not in _nodes(rig)[("Policy", "p-1")]


def test_a_survivor_edge_holding_a_property_the_leg_lacks_is_deleted_and_recreated() -> None:
    rig = GatewayRig()
    _restore(rig, _policy("p-1"), _standard("s-1"), _has("p-1", "s-1", source_ref="x"))

    _restore(
        rig,
        _policy("p-1"),
        _standard("s-1"),
        _has("p-1", "s-1"),
        audit_event_id=_OTHER_EVENT_ID,
    )

    assert _edges(rig)[_edge_key("p-1", "s-1")] == {"identity": "HAS:p-1->s-1"}
    assert _logged(rig)[3:] == ["DeleteEdge", "UpsertEdge"]


# -- legacy (identity-less) edges, stamped inside the same set -----------------------------------


def _legacy_graph(rig: GatewayRig) -> None:
    """What a pre-gateway restore wrote: nodes and identity-less edges, nothing in the log."""
    graph = rig.graphs.open(_NATIVE)
    graph.seed_node(("Policy",), "p-1", {"name": "one"})
    graph.seed_node(("Policy",), "p-2", {"name": "two"})
    graph.seed_node(("Standard",), "s-1", {"name": "std"})
    graph.seed_legacy_edge(("HAS", "Policy", "p-1", "Standard", "s-1"), {})
    graph.seed_legacy_edge(("HAS", "Policy", "p-2", "Standard", "s-1"), {})


def test_a_legacy_edge_the_leg_keeps_is_stamped_and_merged_not_duplicated() -> None:
    rig = GatewayRig()
    _legacy_graph(rig)

    _restore(
        rig,
        _policy("p-1", name="one"),
        _policy("p-2", name="two"),
        _standard("s-1", name="std"),
        _has("p-1", "s-1"),
        _has("p-2", "s-1"),
    )

    assert set(_edges(rig)) == {_edge_key("p-1", "s-1"), _edge_key("p-2", "s-1")}
    assert _logged(rig) == ["StampEdgeIdentity", "StampEdgeIdentity"]  # the upserts were no-ops


def test_a_legacy_edge_the_leg_lacks_is_stamped_then_deleted() -> None:
    rig = GatewayRig()
    _legacy_graph(rig)

    _restore(
        rig,
        _policy("p-1", name="one"),
        _policy("p-2", name="two"),
        _standard("s-1", name="std"),
        _has("p-1", "s-1"),
    )

    assert set(_edges(rig)) == {_edge_key("p-1", "s-1")}
    assert _logged(rig) == ["StampEdgeIdentity", "StampEdgeIdentity", "DeleteEdge"]


def test_legacy_edges_of_a_node_the_leg_lacks_are_not_stamped_they_go_with_the_node() -> None:
    rig = GatewayRig()
    _legacy_graph(rig)

    _restore(rig, _policy("p-1", name="one"), _standard("s-1", name="std"), _has("p-1", "s-1"))

    assert set(_nodes(rig)) == {("Policy", "p-1"), ("Standard", "s-1")}
    assert set(_edges(rig)) == {_edge_key("p-1", "s-1")}
    assert _logged(rig) == ["StampEdgeIdentity", "DeleteNode"]


def test_a_merging_leg_only_gets_the_stamps_and_keeps_everything_else() -> None:
    rig = GatewayRig()
    _restore(rig, _policy("p-1"), _standard("s-1"))
    merge = rig.graphs.open(_MERGE)
    merge.seed_node(("Policy",), "keep-1", {})
    merge.seed_node(("Standard",), "keep-2", {})
    merge.seed_legacy_edge(("HAS", "Policy", "keep-1", "Standard", "keep-2"), {})

    _restore(
        rig,
        _policy("p-1"),
        _standard("s-1"),
        merge=(UpsertNode(label="Capability", id="m2"),),
        audit_event_id=_OTHER_EVENT_ID,
    )

    assert _logged(rig, _MERGE)[-2:] == ["StampEdgeIdentity", "UpsertNode"]
    assert ("Policy", "keep-1") in merge.nodes
    assert ("HAS", "Policy", "keep-1", "Standard", "keep-2", "HAS:keep-1->keep-2") in merge.edges


# -- exactness and replay ------------------------------------------------------------------------


def test_restoring_over_a_different_version_equals_restoring_that_version_fresh() -> None:
    version_two = (
        _policy("p-1", name="one v2"),
        _policy("p-3", name="three"),
        _standard("s-1", name="std"),
        _standard("s-2"),
        _has("p-1", "s-1"),
        _has("p-3", "s-2"),
    )
    fresh = GatewayRig()
    _restore(fresh, *version_two)
    layered = GatewayRig()
    _seeded(layered)

    _restore(layered, *version_two, audit_event_id=_OTHER_EVENT_ID)

    assert canonical_digest(layered.graphs.open(_NATIVE)) == canonical_digest(
        fresh.graphs.open(_NATIVE)
    )


def test_replaying_the_log_from_the_start_rebuilds_the_same_graph_after_a_replace() -> None:
    rig = GatewayRig()
    _seeded(rig)
    _restore(
        rig,
        _policy("p-1", name="one", extra="surplus"),
        _standard("s-1"),
        _has("p-1", "s-1"),
        audit_event_id=_OTHER_EVENT_ID,
    )
    _restore(
        rig,
        _policy("p-1", name="one"),
        _standard("s-1", name="std"),
        _has("p-1", "s-1"),
        audit_event_id="1a2b3c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d",
    )
    live = canonical_digest(rig.graphs.open(_NATIVE))
    rig.graphs.open(_NATIVE).flush()

    rig.restart().replay_graph(_NATIVE)

    assert canonical_digest(rig.graphs.open(_NATIVE)) == live


def test_replaying_after_a_legacy_replace_rebuilds_the_graph_when_the_leg_touched_everything() -> (
    None
):
    rig = GatewayRig()
    _legacy_graph(rig)
    _restore(
        rig,
        _policy("p-1", name="one v2"),
        _policy("p-2", name="two v2"),
        _standard("s-1", name="std v2"),
        _has("p-1", "s-1", source_ref="a"),
        _has("p-2", "s-1", source_ref="b"),
    )
    live = canonical_digest(rig.graphs.open(_NATIVE))
    rig.graphs.open(_NATIVE).flush()

    rig.restart().replay_graph(_NATIVE)

    assert canonical_digest(rig.graphs.open(_NATIVE)) == live


def test_legacy_elements_the_leg_leaves_as_they_are_are_not_logged_so_replay_omits_them() -> None:
    """The limit CA states: data that predates the log is rebuilt only where the log wrote it."""
    rig = GatewayRig()
    _legacy_graph(rig)
    _restore(
        rig,
        _policy("p-1", name="one"),
        _policy("p-2", name="two"),
        _standard("s-1", name="std"),
        _has("p-1", "s-1"),
        _has("p-2", "s-1"),
    )
    rig.graphs.open(_NATIVE).flush()

    rig.restart().replay_graph(_NATIVE)

    assert _nodes(rig) == {}  # nothing but the two stamps (no-ops on an empty graph) was logged


# -- atomicity of the expansion ------------------------------------------------------------------


def test_a_rejected_set_logs_no_stamp_and_leaves_the_legacy_edges_alone() -> None:
    rig = GatewayRig()
    _legacy_graph(rig)

    with pytest.raises(StaleGraphStateError):
        _restore(
            rig,
            _policy("p-1", name="one"),
            _standard("s-1", name="std"),
            _has("p-1", "s-1"),
            merge_preconditions=(ExpectedPosition(position=7),),
        )

    assert rig.store.entries == {}
    assert all(key[5] == "" for key in _edges(rig))  # still identity-less


def test_a_live_node_outside_the_allow_list_rejects_the_whole_set() -> None:
    rig = GatewayRig()
    rig.graphs.open(_NATIVE).seed_node(("NotInTheSchema",), "x", {})

    with pytest.raises(UnlistedNameError):
        _restore(rig, _policy("p-1"), _standard("s-1"))

    assert rig.store.entries == {}


# -- at the group's position ---------------------------------------------------------------------


class _Gate:
    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self._first = True
        self._guard = threading.Lock()

    def __call__(self, graph: str) -> None:
        del graph
        with self._guard:
            hold, self._first = self._first, False
        if hold:
            self.entered.set()
            assert self.release.wait(_WAIT_SECONDS)


def test_a_write_committed_before_the_set_is_seen_by_its_expansion() -> None:
    rig = GatewayRig()
    _seeded(rig)
    gate = _Gate()
    rig.store.append_gate = gate
    write = threading.Thread(
        target=lambda: rig.gateway.submit_group(
            MutationGroup(
                graph=_NATIVE,
                audit_event_id=_OTHER_EVENT_ID,
                primitives=(_policy("late-arrival"),),
            )
        ),
        daemon=True,
    )
    write.start()
    assert gate.entered.wait(_WAIT_SECONDS)
    outcomes: list[MultiGraphOutcome] = []
    restore = threading.Thread(
        target=lambda: outcomes.append(
            _restore(
                rig,
                _policy("p-1", name="one"),
                _policy("p-2", name="two"),
                _standard("s-1", name="std"),
                _has("p-1", "s-1"),
                _has("p-2", "s-1"),
                audit_event_id="1a2b3c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d",
            )
        ),
        daemon=True,
    )
    restore.start()
    restore.join(_BLOCKED_PROBE_SECONDS)
    blocked = restore.is_alive()

    gate.release.set()
    write.join(_WAIT_SECONDS)
    restore.join(_WAIT_SECONDS)

    assert blocked
    assert outcomes[0].legs[0].status == "applied"
    assert ("Policy", "late-arrival") not in _nodes(rig)  # the expansion saw it and deleted it


def test_a_write_after_the_set_is_not_lost() -> None:
    rig = GatewayRig()
    _seeded(rig)
    _restore(rig, _policy("p-1", name="one"), _standard("s-1"), audit_event_id=_OTHER_EVENT_ID)

    rig.gateway.submit_group(
        MutationGroup(
            graph=_NATIVE, audit_event_id=_AUDIT_EVENT_ID, primitives=(_policy("after-the-set"),)
        )
    )

    assert ("Policy", "after-the-set") in _nodes(rig)


# -- logging ---------------------------------------------------------------------------------


def test_an_expansion_is_logged_with_its_size_and_never_its_content(
    make_emitter: MakeEmitter, read_lines: ReadLines
) -> None:
    emitter, log_path = make_emitter()
    rig = GatewayRig(emitter)
    _seeded(rig)
    secret = "sentinel-secret-name"
    _restore(rig, _policy("p-1", name=secret), audit_event_id=_OTHER_EVENT_ID, merge=_merge_only())
    emitter.flush()

    expansions = [line for line in read_lines(log_path) if line.get("action") == "expand_leg"]
    native = [line for line in expansions if line["graph"] == _NATIVE]
    assert [(line["outcome"], line["audit_event_id"]) for line in native] == [
        ("success", _OTHER_EVENT_ID)
    ]
    assert native[0]["entry_count"] == 2  # p-2 and s-1 deleted; their edges go with them
    assert secret not in str(expansions)


def _merge_only() -> tuple[Primitive, ...]:
    return (UpsertNode(label="Capability", id="m-other"),)
