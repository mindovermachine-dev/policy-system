"""No-op suppression: a mutation that would not change the graph is never logged (#206 S6).

AC-BI-004. The gateway reads the current state of every node and edge the group names (batched,
one query per label or type and chunk), then walks the group over that state with an overlay so a
primitive sees the effect of the ones before it. Values compare by type and value (`1` differs from
`1.0`, `True` from `1`, list order matters).
"""

from __future__ import annotations

import json
import math
from typing import TYPE_CHECKING, cast

import pytest
from pydantic import ValidationError

from graph_gateway._fakes import GatewayRig
from ps_service.graph_gateway.errors import MissingTargetError
from ps_service.graph_gateway.gateway import GatewaySettings
from ps_service.graph_gateway.graph_reader import STATE_READ_CHUNK_ROWS
from ps_service.graph_gateway.models import (
    DeleteEdge,
    DeleteNode,
    GroupOutcome,
    MergeProperty,
    MutationGroup,
    NodeRef,
    Primitive,
    RemoveProperty,
    UpsertEdge,
    UpsertNode,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from ps_service.logging import LogEmitter

    MakeEmitter = Callable[[], tuple[LogEmitter, Path]]
    ReadLines = Callable[[Path], list[dict[str, object]]]

_AUDIT_EVENT_ID = "3f2b8c1e-5d4a-4b7e-9a61-0c2d7e8f9a10"
_GRAPH = "compliance"


def _node(node_id: str, **props: object) -> UpsertNode:
    return UpsertNode(label="Capability", id=node_id, properties=props)


def _edge(identity: str = "e-1", **props: object) -> UpsertEdge:
    return UpsertEdge(
        type="HAS",
        identity=identity,
        source=NodeRef(label="Policy", id="p-1"),
        target=NodeRef(label="Standard", id="s-1"),
        properties=props,
    )


def _submit(rig: GatewayRig, *primitives: Primitive) -> GroupOutcome:
    return rig.gateway.submit_group(
        MutationGroup(graph=_GRAPH, audit_event_id=_AUDIT_EVENT_ID, primitives=primitives)
    )


def _logged(rig: GatewayRig) -> int:
    return rig.store.last_position(_GRAPH)


def _writes(rig: GatewayRig) -> int:
    return len([q for q in rig.graphs.open(_GRAPH).queries if q.kind == "write"])


def _rig_with_edge() -> GatewayRig:
    rig = GatewayRig()
    _submit(
        rig,
        UpsertNode(label="Policy", id="p-1"),
        UpsertNode(label="Standard", id="s-1"),
        _edge(weight=1),
    )
    return rig


def test_group_whose_every_mutation_is_a_noop_is_not_logged_and_returns_unchanged() -> None:
    rig = GatewayRig()
    _submit(rig, _node("cap-1", name="a"), _node("cap-2", name="b"))
    writes_before = _writes(rig)

    outcome = _submit(rig, _node("cap-1", name="a"), _node("cap-2", name="b"))

    assert outcome.status == "unchanged"
    assert (outcome.first_position, outcome.last_position) == (None, None)
    assert _logged(rig) == 2
    assert len(rig.store.groups) == 1  # no group, hence no audit link, for the no-op
    assert _writes(rig) == writes_before
    assert rig.store.read_applied_position(_GRAPH) == 2


def test_noop_mutations_dropped_from_partly_effective_group() -> None:
    rig = GatewayRig()
    _submit(rig, _node("cap-1", name="a"))

    outcome = _submit(rig, _node("cap-1", name="a"), _node("cap-2", name="b"))

    assert (outcome.status, outcome.first_position, outcome.last_position) == ("applied", 2, 2)
    assert [entry.identity for entry in rig.store.entries[_GRAPH]] == ["cap-1", "cap-2"]
    assert rig.store.read_applied_position(_GRAPH) == 2


def test_second_identical_submission_is_not_logged() -> None:
    rig = GatewayRig()
    first = _submit(rig, _node("cap-1", name="a", tags=["x", "y"], weight=2.5))

    second = _submit(rig, _node("cap-1", name="a", tags=["x", "y"], weight=2.5))

    assert (first.status, second.status) == ("applied", "unchanged")
    assert _logged(rig) == 1


def test_same_node_with_different_embedding_is_logged() -> None:
    rig = GatewayRig()
    _submit(rig, UpsertNode(label="Capability", id="c", embedding=(1.0, 2.0)))

    outcome = _submit(rig, UpsertNode(label="Capability", id="c", embedding=(1.0, 2.5)))

    assert outcome.status == "applied"
    assert rig.graphs.open(_GRAPH).nodes[("Capability", "c")]["embedding"] == [1.0, 2.5]


def test_same_node_with_same_embedding_is_noop() -> None:
    rig = GatewayRig()
    _submit(rig, UpsertNode(label="Capability", id="c", embedding=(1.0, 2.0)))

    outcome = _submit(rig, UpsertNode(label="Capability", id="c", embedding=(1.0, 2.0)))

    assert outcome.status == "unchanged"


def test_embedding_that_differs_only_in_the_sign_of_zero_is_logged() -> None:
    rig = GatewayRig()
    _submit(rig, UpsertNode(label="Capability", id="c", embedding=(0.0, 1.0)))

    outcome = _submit(rig, UpsertNode(label="Capability", id="c", embedding=(-0.0, 1.0)))

    assert outcome.status == "applied"
    stored = rig.graphs.open(_GRAPH).nodes[("Capability", "c")]["embedding"]
    assert isinstance(stored, list)
    assert math.copysign(1.0, cast("float", stored[0])) == -1.0


def test_omitted_embedding_leaves_an_embedded_node_unchanged_when_properties_match() -> None:
    rig = GatewayRig()
    _submit(rig, UpsertNode(label="Capability", id="c", properties={"a": 1}, embedding=(1.0,)))

    assert _submit(rig, _node("c", a=1)).status == "unchanged"


@pytest.mark.parametrize(
    ("first", "second"),
    [
        (1, 1.0),
        (1.0, 1),
        (True, 1),
        (1, True),
        (["a", "b"], ["b", "a"]),
        ([1, 2], [1.0, 2.0]),
        ("1", 1),
    ],
)
def test_values_that_differ_in_type_or_list_order_are_logged(first: object, second: object) -> None:
    rig = GatewayRig()
    _submit(rig, _node("c", v=first))

    outcome = _submit(rig, _node("c", v=second))

    assert outcome.status == "applied"
    assert _logged(rig) == 2


@pytest.mark.parametrize("value", [1, 1.0, True, "x", [1, 2], ["b", "a"], [1.5], [False, True]])
def test_the_same_value_round_trips_through_the_graph_as_a_noop(value: object) -> None:
    rig = GatewayRig()
    _submit(rig, _node("c", v=value))

    assert _submit(rig, _node("c", v=value)).status == "unchanged"


def test_merge_property_with_equal_values_is_a_noop_and_with_one_new_value_is_logged() -> None:
    rig = GatewayRig()
    _submit(rig, _node("c", a=1, b=2))

    same = _submit(rig, MergeProperty(label="Capability", id="c", properties={"a": 1}))
    partly = _submit(rig, MergeProperty(label="Capability", id="c", properties={"a": 1, "b": 3}))

    assert (same.status, partly.status) == ("unchanged", "applied")
    assert rig.store.entries[_GRAPH][-1].content["properties"] == {"a": 1, "b": 3}


def test_remove_property_of_an_absent_key_is_a_noop() -> None:
    rig = GatewayRig()
    _submit(rig, _node("c", a=1))

    absent = _submit(rig, RemoveProperty(label="Capability", id="c", keys=("zzz",)))
    present = _submit(rig, RemoveProperty(label="Capability", id="c", keys=("zzz", "a")))

    assert (absent.status, present.status) == ("unchanged", "applied")
    assert rig.graphs.open(_GRAPH).nodes[("Capability", "c")] == {}


def test_remove_property_on_an_absent_node_is_a_noop_not_an_error() -> None:
    rig = GatewayRig()

    outcome = _submit(rig, RemoveProperty(label="Capability", id="ghost", keys=("a",)))

    assert outcome.status == "unchanged"
    assert rig.store.entries == {}


def test_delete_node_of_an_absent_node_is_a_noop_not_an_error() -> None:
    rig = GatewayRig()

    outcome = _submit(rig, DeleteNode(label="Capability", id="ghost"))

    assert outcome.status == "unchanged"
    assert rig.store.entries == {}


def test_delete_edge_of_an_absent_edge_is_a_noop_not_an_error() -> None:
    rig = _rig_with_edge()
    absent = DeleteEdge(
        type="HAS",
        identity="never-existed",
        source=NodeRef(label="Policy", id="p-1"),
        target=NodeRef(label="Standard", id="s-1"),
    )

    outcome = _submit(rig, absent)

    assert outcome.status == "unchanged"
    assert len(rig.graphs.open(_GRAPH).edges) == 1


def test_edge_with_equal_properties_is_a_noop_and_with_different_ones_is_logged() -> None:
    rig = _rig_with_edge()
    logged_before = _logged(rig)

    same = _submit(rig, _edge(weight=1))
    different = _submit(rig, _edge(weight=2))
    float_instead = _submit(rig, _edge(weight=2.0))

    assert (same.status, different.status, float_instead.status) == (
        "unchanged",
        "applied",
        "applied",
    )
    assert _logged(rig) == logged_before + 2


def test_a_second_edge_identity_between_the_same_nodes_is_logged() -> None:
    rig = _rig_with_edge()

    outcome = _submit(rig, _edge("e-2", weight=1))

    assert outcome.status == "applied"
    assert len(rig.graphs.open(_GRAPH).edges) == 2


def test_upsert_followed_by_identical_upsert_in_the_same_group_drops_the_second() -> None:
    rig = GatewayRig()

    outcome = _submit(rig, _node("c", a=1), _node("c", a=1))

    assert (outcome.first_position, outcome.last_position) == (1, 1)
    assert _logged(rig) == 1


def test_delete_followed_by_reupsert_in_the_same_group_logs_both() -> None:
    rig = GatewayRig()
    _submit(rig, _node("c", a=1))

    outcome = _submit(rig, DeleteNode(label="Capability", id="c"), _node("c", a=1))

    assert (outcome.first_position, outcome.last_position) == (2, 3)
    assert rig.graphs.open(_GRAPH).nodes[("Capability", "c")] == {"a": 1}


def test_upsert_then_delete_of_a_new_node_in_one_group_logs_both() -> None:
    rig = GatewayRig()

    outcome = _submit(rig, _node("c", a=1), DeleteNode(label="Capability", id="c"))

    assert outcome.last_position == 2
    assert rig.graphs.open(_GRAPH).nodes == {}


def test_deleting_a_node_in_the_group_makes_its_edge_delete_a_noop() -> None:
    rig = _rig_with_edge()
    delete_edge = DeleteEdge(
        type="HAS",
        identity="e-1",
        source=NodeRef(label="Policy", id="p-1"),
        target=NodeRef(label="Standard", id="s-1"),
    )

    outcome = _submit(rig, DeleteNode(label="Standard", id="s-1"), delete_edge)

    assert (outcome.first_position, outcome.last_position) == (4, 4)


def test_merge_property_on_an_absent_node_is_still_rejected_not_dropped() -> None:
    rig = GatewayRig()

    with pytest.raises(MissingTargetError):
        _submit(rig, MergeProperty(label="Capability", id="ghost", properties={"a": 1}))


def test_reads_are_chunked_at_the_state_read_bound_and_a_repeat_issues_no_write() -> None:
    batch, total = 500, 1201
    chunk = min(batch, STATE_READ_CHUNK_ROWS)  # a state row can carry an embedding
    rig = GatewayRig(settings=GatewaySettings(batch_size=batch))
    nodes = [_node(f"cap-{index}", n=index) for index in range(total)]

    _submit(rig, *nodes)

    graph = rig.graphs.open(_GRAPH)
    reads = [q for q in graph.queries if q.kind == "read"]
    assert len(reads) == math.ceil(total / chunk)
    assert all(len(_row_list(q.params)) <= chunk for q in reads)
    writes_before = _writes(rig)

    repeat = _submit(rig, *nodes)

    assert repeat.status == "unchanged"
    assert _writes(rig) == writes_before
    assert len([q for q in graph.queries if q.kind == "read"]) == 2 * math.ceil(total / chunk)


def _row_list(params: dict[str, object]) -> list[object]:
    rows = params["rows"]
    assert isinstance(rows, list)
    return list(rows)  # pyright: ignore[reportUnknownArgumentType]


def test_unchanged_outcome_carries_no_positions_and_applied_requires_them() -> None:
    GroupOutcome(graph=_GRAPH, first_position=None, last_position=None, status="unchanged")
    GroupOutcome(graph=_GRAPH, first_position=1, last_position=2, status="applied")
    with pytest.raises(ValidationError):
        GroupOutcome(graph=_GRAPH, first_position=1, last_position=1, status="unchanged")
    with pytest.raises(ValidationError):
        GroupOutcome(graph=_GRAPH, first_position=None, last_position=None, status="applied")


def test_unchanged_group_is_logged_with_graph_and_audit_event_only(
    make_emitter: MakeEmitter, read_lines: ReadLines
) -> None:
    emitter, log_path = make_emitter()
    rig = GatewayRig(emitter)
    secret = "sentinel-secret-payload"
    _submit(rig, _node("c", note=secret))

    _submit(rig, _node("c", note=secret))
    emitter.flush()

    lines = [line for line in read_lines(log_path) if line.get("action") == "apply_group"]
    unchanged = [line for line in lines if line["outcome"] == "unchanged"]
    assert len(unchanged) == 1
    assert (unchanged[0]["graph"], unchanged[0]["entry_count"]) == (_GRAPH, 0)
    assert secret not in json.dumps(unchanged[0])
