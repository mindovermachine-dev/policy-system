"""A graph that failed or is in a replay refuses writes; other graphs do not (#207 S11, AC-RD-011).

Nothing is repaired: a failed replay leaves the rebuilt content exactly as built, and the graph
stays closed until a later replay completes.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import pytest

from graph_gateway._fakes import FakeQueryResult, GatewayRig, InMemoryGraph
from ps_service.graph_gateway.digest import canonical_digest
from ps_service.graph_gateway.errors import (
    GraphApplyBlockedError,
    GraphApplyError,
    GraphDigestMismatchError,
    GraphReplayGatedError,
)
from ps_service.graph_gateway.models import DigestCheckpoint, MutationGroup, UpsertNode

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
_OTHER = "other"
_WAIT_SECONDS = 5.0
_WRONG = "sha256:" + "0" * 64


def _node(node_id: str) -> UpsertNode:
    return UpsertNode(label="Capability", id=node_id, properties={"name": node_id})


def _group(graph: str, *nodes: UpsertNode, checkpoint_requested: bool = False) -> MutationGroup:
    return MutationGroup(
        graph=graph,
        audit_event_id=_AUDIT_EVENT_ID,
        primitives=nodes,
        checkpoint_requested=checkpoint_requested,
    )


def _failed_replay_rig() -> tuple[GatewayRig, str]:
    """A rig whose graph failed replay verification at position 4, and the right digest for it."""
    rig = GatewayRig()
    rig.gateway.submit_group(
        _group(_GRAPH, *(_node(f"cap-{i}") for i in range(1, 7)), checkpoint_requested=False)
    )
    rig.gateway.submit_group(_group(_OTHER, _node("o-1")))
    right = _digest_after(4)
    rig.store.checkpoints[(_GRAPH, 4)] = DigestCheckpoint(
        graph=_GRAPH, position=4, canonical_digest=_WRONG
    )
    rig.graphs.open(_GRAPH).flush()
    gateway = rig.restart()
    with pytest.raises(GraphDigestMismatchError):
        gateway.replay_graph(_GRAPH)
    return rig, right


def _digest_after(position: int) -> str:
    """The digest the graph has after the first `position` entries (built on a scratch rig)."""
    scratch = GatewayRig()
    scratch.gateway.submit_group(
        _group(_GRAPH, *(_node(f"cap-{i}") for i in range(1, position + 1)))
    )
    return canonical_digest(scratch.graphs.open(_GRAPH))


def test_replay_failure_gates_writes_to_that_graph_with_a_typed_error() -> None:
    rig, _ = _failed_replay_rig()
    logged = rig.store.logged_count(_GRAPH)

    with pytest.raises(GraphReplayGatedError) as raised:
        rig.gateway.submit_group(_group(_GRAPH, _node("cap-new")))

    error = raised.value
    assert isinstance(error, GraphApplyError)
    assert not isinstance(error, GraphApplyBlockedError)
    assert (error.graph, error.position) == (_GRAPH, 4)
    assert _GRAPH in str(error)
    assert "4" in str(error)
    assert rig.store.logged_count(_GRAPH) == logged  # nothing was logged


def test_the_staged_in_transaction_path_is_gated_too_and_leaks_no_lock() -> None:
    rig, right = _failed_replay_rig()
    transaction = rig.store.begin()
    audit_event_id = transaction.record_audit()
    group = MutationGroup(
        graph=_GRAPH, audit_event_id=audit_event_id, primitives=(_node("cap-new"),)
    )

    with pytest.raises(GraphReplayGatedError):
        rig.gateway.submit_group_in_transaction(transaction.cursor, group)

    transaction.rollback()
    rig.store.checkpoints[(_GRAPH, 4)] = DigestCheckpoint(
        graph=_GRAPH, position=4, canonical_digest=right
    )
    rig.graphs.open(_GRAPH).flush()
    rig.gateway.replay_graph(_GRAPH)
    rig.gateway.submit_group(_group(_GRAPH, _node("cap-new")))  # the lock was not leaked


def test_other_graphs_keep_accepting_writes_while_one_is_gated() -> None:
    rig, _ = _failed_replay_rig()

    outcome = rig.gateway.submit_group(_group(_OTHER, _node("o-2")))

    assert outcome.status == "applied"


def test_replay_failure_leaves_the_rebuilt_content_untouched() -> None:
    rig, _ = _failed_replay_rig()

    assert {node_id for _, node_id in rig.graphs.open(_GRAPH).nodes} == {
        f"cap-{i}" for i in range(1, 5)
    }  # exactly what was applied through the checkpoint, nothing flushed or repaired
    assert rig.store.markers[_GRAPH] == 6  # the original marker, never lowered or moved


def test_catch_up_on_a_gated_graph_refuses() -> None:
    rig, _ = _failed_replay_rig()

    with pytest.raises(GraphReplayGatedError):
        rig.gateway.catch_up(_GRAPH)


def test_is_caught_up_is_false_for_a_gated_graph() -> None:
    rig, _ = _failed_replay_rig()

    assert rig.store.read_applied_position(_GRAPH) == rig.store.last_position(_GRAPH)
    assert rig.gateway.is_caught_up(_GRAPH) is False
    assert rig.gateway.is_caught_up(_OTHER) is True


def test_recover_lists_a_gated_graph_as_gated() -> None:
    rig, _ = _failed_replay_rig()
    rig.store.markers[_GRAPH] = 0  # make the graph look behind its log

    result = rig.gateway.recover()

    assert _GRAPH in result.gated


def test_a_gated_graph_is_released_by_a_later_successful_replay() -> None:
    rig, right = _failed_replay_rig()
    rig.store.checkpoints[(_GRAPH, 4)] = DigestCheckpoint(
        graph=_GRAPH, position=4, canonical_digest=right
    )  # the operator fixed the cause
    rig.graphs.open(_GRAPH).flush()  # and emptied the graph

    report = rig.gateway.replay_graph(_GRAPH)

    assert report.verified_position == 4
    assert rig.gateway.is_caught_up(_GRAPH) is True
    assert rig.gateway.submit_group(_group(_GRAPH, _node("cap-new"))).status == "applied"


def test_gated_error_messages() -> None:
    waiting = GraphReplayGatedError(_GRAPH)
    failed = GraphReplayGatedError(_GRAPH, 4, failed=True)

    assert _GRAPH in str(waiting)
    assert "blocked by a logged entry" not in str(waiting)
    assert "4" in str(failed)
    assert _GRAPH in str(failed)


@dataclass
class _ParkedGraph(InMemoryGraph):
    """A graph whose first write parks until the test releases it."""

    parked: threading.Event = field(default_factory=threading.Event)
    release: threading.Event = field(default_factory=threading.Event)

    def query(self, q: str, params: dict[str, object] | None = None) -> FakeQueryResult:
        if q.startswith("UNWIND") and not self.parked.is_set():
            self.parked.set()
            assert self.release.wait(_WAIT_SECONDS), "the test never released the replay"
        return super().query(q, params)


def test_writes_are_refused_at_once_while_the_graph_is_being_replayed() -> None:
    rig = GatewayRig()
    rig.gateway.submit_group(_group(_GRAPH, _node("cap-1"), _node("cap-2")))
    parked = _ParkedGraph(events=rig.events)
    rig.graphs.graphs[_GRAPH] = parked
    gateway = rig.restart()
    replay = threading.Thread(target=lambda: gateway.replay_graph(_GRAPH), daemon=True)
    replay.start()
    assert parked.parked.wait(_WAIT_SECONDS)

    with pytest.raises(GraphReplayGatedError) as raised:
        gateway.submit_group(_group(_GRAPH, _node("cap-3")))

    assert raised.value.position is None
    assert rig.store.logged_count(_GRAPH) == 2
    parked.release.set()
    replay.join(_WAIT_SECONDS)
    assert gateway.submit_group(_group(_GRAPH, _node("cap-3"))).status == "applied"


def test_replay_failure_is_logged_with_the_class_and_the_failed_position_only(
    make_emitter: MakeEmitter, read_lines: ReadLines
) -> None:
    emitter, log_path = make_emitter()
    rig = GatewayRig(emitter=emitter)
    rig.gateway.submit_group(_group(_GRAPH, *(_node(f"cap-{i}") for i in range(1, 5))))
    rig.store.checkpoints[(_GRAPH, 3)] = DigestCheckpoint(
        graph=_GRAPH, position=3, canonical_digest=_WRONG
    )
    rig.graphs.open(_GRAPH).flush()

    with pytest.raises(GraphDigestMismatchError):
        rig.restart().replay_graph(_GRAPH)
    emitter.flush()

    (line,) = [e for e in read_lines(log_path) if e["action"] == "replay_graph"]
    assert line["outcome"] == "failure"
    assert (line["graph"], line["failed_position"]) == (_GRAPH, 3)
    assert line["error_class"] == "GraphDigestMismatchError"
    assert _WRONG not in str(line)
