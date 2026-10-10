"""The checkpoint recorder's edge cases (#207 S6, AC-RD-003).

A checkpoint is taken only for a group that was applied, under the graph's lock, and a failure to
take it never fails a group that is already committed. The store keeps the first checkpoint at a
position (insert-only).
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import pytest
import redis.exceptions

from graph_gateway._fakes import FakeQueryResult, Fault, GatewayRig, InMemoryGraph
from ps_service.graph_gateway.digest import canonical_digest
from ps_service.graph_gateway.errors import GraphLogPersistenceError, GraphLogUnavailableError
from ps_service.graph_gateway.models import GroupOutcome, MutationGroup, UpsertNode

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
_WAIT_SECONDS = 5.0
_BLOCKED_PROBE_SECONDS = 0.3
_DOWN = redis.exceptions.ConnectionError("down")


def _node(node_id: str) -> UpsertNode:
    return UpsertNode(label="Capability", id=node_id, properties={"name": node_id})


def _group(*nodes: UpsertNode, audit_event_id: str = _AUDIT_EVENT_ID) -> MutationGroup:
    return MutationGroup(
        graph=_GRAPH, audit_event_id=audit_event_id, primitives=nodes, checkpoint_requested=True
    )


def test_checkpoint_is_not_recorded_when_apply_is_pending() -> None:
    rig = GatewayRig()
    rig.graphs.open(_GRAPH).fail_on_write(_DOWN)

    outcome = rig.gateway.submit_group(_group(_node("cap-1")))

    assert outcome.status == "committed_apply_pending"
    assert outcome.checkpoint == "not_recorded"
    assert outcome.checkpoint_position is None
    assert rig.store.checkpoints == {}


def test_checkpoint_is_not_recorded_for_an_unchanged_group() -> None:
    rig = GatewayRig()
    rig.gateway.submit_group(_group(_node("cap-1")))
    rig.store.checkpoints.clear()

    outcome = rig.gateway.submit_group(_group(_node("cap-1")))

    assert outcome.status == "unchanged"
    assert outcome.checkpoint == "not_recorded"
    assert rig.store.checkpoints == {}


def test_checkpoint_recording_failure_does_not_fail_a_committed_group() -> None:
    rig = GatewayRig()
    rig.store.checkpoint_fault = Fault(GraphLogUnavailableError())

    outcome = rig.gateway.submit_group(_group(_node("cap-1")))

    assert outcome.status == "applied"
    assert outcome.checkpoint == "not_recorded"
    assert outcome.checkpoint_position is None
    assert rig.store.checkpoints == {}
    assert rig.store.read_applied_position(_GRAPH) == 1


@dataclass
class _DigestFailingGraph(InMemoryGraph):
    """A graph that answers writes and state reads but is down for the digest scans."""

    def query(self, q: str, params: dict[str, object] | None = None) -> FakeQueryResult:
        if q.startswith(("MATCH (n) WHERE id(n) > $after", "MATCH (s)-[r]->(t) WHERE id(r)")):
            raise _DOWN
        return super().query(q, params)


def test_a_digest_that_cannot_be_taken_does_not_fail_a_committed_group() -> None:
    rig = GatewayRig()
    rig.graphs.graphs[_GRAPH] = _DigestFailingGraph(events=rig.events)

    outcome = rig.gateway.submit_group(_group(_node("cap-1")))

    assert outcome.status == "applied"
    assert outcome.checkpoint == "not_recorded"
    assert rig.store.checkpoints == {}


def test_checkpoint_failure_is_logged_by_error_class_only(
    make_emitter: MakeEmitter, read_lines: ReadLines
) -> None:
    emitter, log_path = make_emitter()
    rig = GatewayRig(emitter=emitter)
    rig.store.checkpoint_fault = Fault(GraphLogUnavailableError())

    rig.gateway.submit_group(_group(_node("cap-1")))
    emitter.flush()

    (failure,) = [line for line in read_lines(log_path) if line["action"] == "checkpoint"]
    assert failure["outcome"] == "failure"
    assert failure["graph"] == _GRAPH
    assert failure["error_class"] == "GraphLogUnavailableError"
    assert failure["last_position"] == 1
    assert "cap-1" not in str(failure)


def test_a_recorded_checkpoint_is_logged_with_its_position(
    make_emitter: MakeEmitter, read_lines: ReadLines
) -> None:
    emitter, log_path = make_emitter()
    rig = GatewayRig(emitter=emitter)

    rig.gateway.submit_group(_group(_node("cap-1"), _node("cap-2")))
    emitter.flush()

    (recorded,) = [line for line in read_lines(log_path) if line["action"] == "checkpoint"]
    assert recorded["outcome"] == "success"
    assert recorded["last_position"] == 2


def test_staged_in_transaction_group_records_a_requested_checkpoint_on_complete() -> None:
    rig = GatewayRig()
    transaction = rig.store.begin()
    audit_event_id = transaction.record_audit()

    with rig.gateway.submit_group_in_transaction(
        transaction.cursor, _group(_node("cap-1"), audit_event_id=audit_event_id)
    ) as staged:
        transaction.commit()
        assert rig.store.checkpoints == {}  # nothing is taken before the caller commits
        outcome = staged.complete()

    assert outcome.checkpoint == "recorded"
    assert outcome.checkpoint_position == 1
    assert rig.store.checkpoints[(_GRAPH, 1)].canonical_digest == canonical_digest(
        rig.graphs.open(_GRAPH)
    )


def test_a_second_checkpoint_at_the_same_position_is_refused_and_the_first_is_kept() -> None:
    rig = GatewayRig()
    rig.gateway.submit_group(_group(_node("cap-1")))
    first = rig.store.checkpoints[(_GRAPH, 1)]

    with pytest.raises(GraphLogPersistenceError):
        rig.store.record_digest_checkpoint(_GRAPH, 1, "sha256:" + "0" * 64)

    assert rig.store.checkpoints[(_GRAPH, 1)] == first


@dataclass
class _GatedGraph(InMemoryGraph):
    """A graph whose digest scan parks until the test releases it."""

    scanning: threading.Event = field(default_factory=threading.Event)
    release: threading.Event = field(default_factory=threading.Event)

    def query(self, q: str, params: dict[str, object] | None = None) -> FakeQueryResult:
        if q.startswith("MATCH (n) WHERE id(n) > $after") and not self.scanning.is_set():
            self.scanning.set()
            assert self.release.wait(_WAIT_SECONDS), "the test never released the digest scan"
        return super().query(q, params)


def test_a_concurrent_group_cannot_interleave_with_the_checkpoint_digest() -> None:
    rig = GatewayRig()
    gated = _GatedGraph(events=rig.events)
    rig.graphs.graphs[_GRAPH] = gated
    first_outcomes: list[GroupOutcome] = []
    second_outcomes: list[GroupOutcome] = []
    first = threading.Thread(
        target=lambda: first_outcomes.append(rig.gateway.submit_group(_group(_node("cap-1")))),
        daemon=True,
    )
    first.start()
    assert gated.scanning.wait(_WAIT_SECONDS)
    second = threading.Thread(
        target=lambda: second_outcomes.append(rig.gateway.submit_group(_group(_node("cap-2")))),
        daemon=True,
    )
    second.start()
    second.join(_BLOCKED_PROBE_SECONDS)

    assert second.is_alive()  # waits for the lock while the digest is being taken
    assert rig.store.logged_count(_GRAPH) == 1

    gated.release.set()
    first.join(_WAIT_SECONDS)
    second.join(_WAIT_SECONDS)

    only_first = GatewayRig()
    only_first.gateway.submit_group(_group(_node("cap-1")))
    assert rig.store.checkpoints[(_GRAPH, 1)].canonical_digest == (
        only_first.store.checkpoints[(_GRAPH, 1)].canonical_digest
    )
    assert second_outcomes[0].first_position == 2
