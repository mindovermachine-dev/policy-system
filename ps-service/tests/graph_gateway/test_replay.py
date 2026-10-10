"""Replay rebuilds a graph from the log and verifies it against a checkpoint (#207 S7-S9).

The log is the in-memory store, the graph the in-memory graph; both are the approved boundary
fakes (see `_fakes`). Every test enters at `GraphWriteGateway.replay_graph`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from graph_gateway._fakes import GatewayRig
from ps_service.graph_gateway.digest import canonical_digest
from ps_service.graph_gateway.errors import GraphDigestMismatchError, GraphReplayError
from ps_service.graph_gateway.models import (
    DeleteNode,
    DigestCheckpoint,
    MutationGroup,
    Primitive,
    UpsertNode,
)

if TYPE_CHECKING:
    from pathlib import Path
    from typing import Protocol

    from ps_service.logging import LogEmitter

    class MakeEmitter(Protocol):
        """Call shape of the shared `make_emitter` fixture (`tests/conftest.py`)."""

        def __call__(self) -> tuple[LogEmitter, Path]: ...

    class ReadLines(Protocol):
        """Call shape of the shared `read_lines` fixture (`tests/conftest.py`)."""

        def __call__(self, log_path: Path) -> list[dict[str, object]]: ...


_AUDIT_EVENT_ID = "3f2b8c1e-5d4a-4b7e-9a61-0c2d7e8f9a10"
_GRAPH = "compliance"


def _node(node_id: str, name: str = "n") -> UpsertNode:
    return UpsertNode(label="Capability", id=node_id, properties={"name": name})


def _submit(rig: GatewayRig, *primitives: Primitive, checkpoint_requested: bool = False) -> None:
    rig.gateway.submit_group(
        MutationGroup(
            graph=_GRAPH,
            audit_event_id=_AUDIT_EVENT_ID,
            primitives=primitives,
            checkpoint_requested=checkpoint_requested,
        )
    )


def test_replay_into_an_empty_graph_rebuilds_it_and_matches_the_head_checkpoint() -> None:
    rig = GatewayRig()
    _submit(rig, _node("cap-1", "a"), _node("cap-2", "b"), checkpoint_requested=True)
    expected = rig.store.checkpoints[(_GRAPH, 2)].canonical_digest
    rig.graphs.open(_GRAPH).flush()

    report = rig.restart().replay_graph(_GRAPH)

    assert (report.graph, report.head) == (_GRAPH, 2)
    assert report.verified_position == 2
    assert report.unverified_entries == 0
    assert canonical_digest(rig.graphs.open(_GRAPH)) == expected


def test_replay_advances_the_marker_to_the_head_and_never_lowers_it() -> None:
    rig = GatewayRig()
    _submit(rig, _node("cap-1"), _node("cap-2"), _node("cap-3"), checkpoint_requested=True)
    rig.graphs.open(_GRAPH).flush()
    rig.store.markers[_GRAPH] = 1  # a marker that fell behind (cannot happen in Postgres, but safe)

    rig.restart().replay_graph(_GRAPH)

    assert rig.store.markers[_GRAPH] == 3
    assert all(position <= 3 for _, position in rig.store.marker_history)

    rig.graphs.open(_GRAPH).flush()
    history = len(rig.store.marker_history)
    rig.restart().replay_graph(_GRAPH)

    assert rig.store.markers[_GRAPH] == 3
    assert [p for _, p in rig.store.marker_history[history:]] in ([], [3])


def test_replay_applies_in_sequence_order() -> None:
    rig = GatewayRig()
    _submit(rig, _node("a", "first"), _node("b", "first"))
    _submit(rig, DeleteNode(label="Capability", id="a"))
    _submit(rig, _node("a", "second"), checkpoint_requested=True)
    graph = rig.graphs.open(_GRAPH)
    original_order = list(graph.upsert_order)
    expected = rig.store.checkpoints[(_GRAPH, 4)].canonical_digest
    graph.flush()
    graph.upsert_order.clear()

    rig.restart().replay_graph(_GRAPH)

    assert graph.upsert_order == original_order
    assert graph.nodes[("Capability", "a")] == {"name": "second"}
    assert canonical_digest(graph) == expected


def test_replay_of_an_empty_log_is_an_empty_report() -> None:
    rig = GatewayRig()

    report = rig.gateway.replay_graph(_GRAPH)

    assert (report.head, report.verified_position, report.unverified_entries) == (0, None, 0)


def _log_with_checkpoints_at_3_and_7_and_head_9(rig: GatewayRig) -> None:
    _submit(rig, *(_node(f"cap-{i}") for i in range(1, 4)), checkpoint_requested=True)
    _submit(rig, *(_node(f"cap-{i}") for i in range(4, 8)), checkpoint_requested=True)
    _submit(rig, _node("cap-8"), _node("cap-9"))
    rig.graphs.open(_GRAPH).flush()


def test_replay_verifies_against_the_highest_checkpoint_at_or_below_the_head() -> None:
    rig = GatewayRig()
    _log_with_checkpoints_at_3_and_7_and_head_9(rig)

    report = rig.restart().replay_graph(_GRAPH)

    assert (report.head, report.verified_position, report.unverified_entries) == (9, 7, 2)


def test_a_checkpoint_above_the_head_is_ignored() -> None:
    rig = GatewayRig()
    _log_with_checkpoints_at_3_and_7_and_head_9(rig)
    rig.store.checkpoints[(_GRAPH, 20)] = DigestCheckpoint(
        graph=_GRAPH, position=20, canonical_digest="sha256:" + "0" * 64
    )

    report = rig.restart().replay_graph(_GRAPH)

    assert report.verified_position == 7


def test_digest_mismatch_raises_a_typed_error_naming_graph_and_sequence() -> None:
    rig = GatewayRig()
    _log_with_checkpoints_at_3_and_7_and_head_9(rig)
    wrong = "sha256:" + "0" * 64
    right = rig.store.checkpoints[(_GRAPH, 7)].canonical_digest
    rig.store.checkpoints[(_GRAPH, 7)] = DigestCheckpoint(
        graph=_GRAPH, position=7, canonical_digest=wrong
    )

    with pytest.raises(GraphDigestMismatchError) as raised:
        rig.restart().replay_graph(_GRAPH)

    error = raised.value
    assert isinstance(error, GraphReplayError)
    assert (error.graph, error.position) == (_GRAPH, 7)
    assert _GRAPH in str(error)
    assert "7" in str(error)
    assert wrong not in str(error)
    assert right not in str(error)
    assert (error.expected, error.actual) == (wrong, right)


def test_a_mismatch_stops_before_the_entries_after_the_checkpoint() -> None:
    rig = GatewayRig()
    _log_with_checkpoints_at_3_and_7_and_head_9(rig)
    rig.store.checkpoints[(_GRAPH, 7)] = DigestCheckpoint(
        graph=_GRAPH, position=7, canonical_digest="sha256:" + "0" * 64
    )
    rig.store.markers[_GRAPH] = 0

    with pytest.raises(GraphDigestMismatchError):
        rig.restart().replay_graph(_GRAPH)

    assert len(rig.graphs.open(_GRAPH).nodes) == 7  # nothing past the checkpoint was applied
    assert rig.store.markers[_GRAPH] == 0


def test_entries_after_the_last_checkpoint_are_reported_as_an_unverified_tail() -> None:
    rig = GatewayRig()
    _log_with_checkpoints_at_3_and_7_and_head_9(rig)

    report = rig.restart().replay_graph(_GRAPH)

    assert report.verified_position == 7
    assert report.unverified_entries == report.head - 7 == 2


def test_replay_never_records_a_checkpoint_from_the_rebuilt_graph() -> None:
    rig = GatewayRig()
    _log_with_checkpoints_at_3_and_7_and_head_9(rig)
    before = dict(rig.store.checkpoints)

    rig.restart().replay_graph(_GRAPH)

    assert rig.store.checkpoints == before


def test_replay_of_a_graph_with_no_checkpoint_reports_the_whole_log_unverified() -> None:
    rig = GatewayRig()
    _submit(rig, _node("cap-1"), _node("cap-2"), _node("cap-3"))
    rig.graphs.open(_GRAPH).flush()

    report = rig.restart().replay_graph(_GRAPH)

    assert (report.head, report.verified_position, report.unverified_entries) == (3, None, 3)
    assert rig.store.checkpoints == {}
    assert len(rig.graphs.open(_GRAPH).nodes) == 3


def test_replay_logs_the_positions_and_the_unverified_tail_count(
    make_emitter: MakeEmitter, read_lines: ReadLines
) -> None:
    emitter, log_path = make_emitter()
    rig = GatewayRig(emitter=emitter)
    _log_with_checkpoints_at_3_and_7_and_head_9(rig)

    rig.restart().replay_graph(_GRAPH)
    emitter.flush()

    (line,) = [entry for entry in read_lines(log_path) if entry["action"] == "replay_graph"]
    assert line["outcome"] == "success"
    assert (line["graph"], line["last_position"]) == (_GRAPH, 9)
    assert (line["verified_position"], line["unverified_entries"]) == (7, 2)
