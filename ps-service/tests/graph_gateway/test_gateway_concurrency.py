"""Per-graph serialization of submitted groups (issue #206 S7, AC-BI-006).

Two groups for one graph must not interleave: positions stay contiguous per group and the graph
sees them in position order. Groups for different graphs do not wait for each other.

These tests use the in-memory log store, whose "append" is not a database. They prove the
in-process lock (lock order, step 1 of the order in `graph_locks`); that ordering also holds
across the Postgres advisory lock is proved only by the `postgres_live` variant in
`test_gateway_live.py`, which cannot run in the implementation sandbox.
"""

from __future__ import annotations

import threading

from graph_gateway._fakes import GatewayRig
from ps_service.graph_gateway.graph_locks import GraphLockRegistry
from ps_service.graph_gateway.models import (
    GroupOutcome,
    MergeProperty,
    MutationGroup,
    UpsertNode,
)

_AUDIT_EVENT_ID = "3f2b8c1e-5d4a-4b7e-9a61-0c2d7e8f9a10"
_WAIT_SECONDS = 5.0
_BLOCKED_PROBE_SECONDS = 0.3


def _group(graph: str, prefix: str, count: int = 2) -> MutationGroup:
    return MutationGroup(
        graph=graph,
        audit_event_id=_AUDIT_EVENT_ID,
        primitives=tuple(UpsertNode(label="Capability", id=f"{prefix}-{i}") for i in range(count)),
    )


class _Gate:
    """Holds the first standalone append of `graph` until released; others pass through."""

    def __init__(self, graph: str) -> None:
        self._graph = graph
        self.entered = threading.Event()
        self.release = threading.Event()
        self._first = True
        self._guard = threading.Lock()

    def __call__(self, graph: str) -> None:
        with self._guard:
            hold = graph == self._graph and self._first
            self._first = self._first and graph != self._graph
        if hold:
            self.entered.set()
            assert self.release.wait(_WAIT_SECONDS)


class _Submitter:
    """Runs `submit_group` on a thread and keeps its outcome."""

    def __init__(self, rig: GatewayRig, group: MutationGroup) -> None:
        self.outcome: GroupOutcome | None = None
        self._rig = rig
        self._group = group
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        self.outcome = self._rig.gateway.submit_group(self._group)

    def start(self) -> None:
        self._thread.start()

    def finishes_within(self, seconds: float) -> bool:
        self._thread.join(seconds)
        return not self._thread.is_alive()


def test_two_groups_on_same_graph_serialize_in_commit_order_fake_store() -> None:
    rig = GatewayRig()
    gate = _Gate("compliance")
    rig.store.append_gate = gate
    first = _Submitter(rig, _group("compliance", "a"))
    second = _Submitter(rig, _group("compliance", "b"))

    first.start()
    assert gate.entered.wait(_WAIT_SECONDS)
    second.start()
    blocked = not second.finishes_within(_BLOCKED_PROBE_SECONDS)
    reads_while_blocked = rig.events.count("graph_read")
    gate.release.set()
    assert first.finishes_within(_WAIT_SECONDS)
    assert second.finishes_within(_WAIT_SECONDS)

    assert blocked  # the second group waited for the first
    assert reads_while_blocked == 1  # ... including its state read, which is under the lock
    assert first.outcome is not None
    assert second.outcome is not None
    assert (first.outcome.first_position, first.outcome.last_position) == (1, 2)
    assert (second.outcome.first_position, second.outcome.last_position) == (3, 4)
    assert [event for event in rig.events if event != "graph_read"] == [
        "log_append",
        "graph_write",
        "log_append",
        "graph_write",
    ]
    graph = rig.graphs.open("compliance")
    assert graph.upsert_order == [
        ("Capability", e.identity) for e in rig.store.entries["compliance"]
    ]
    assert rig.store.read_applied_position("compliance") == 4


def test_group_on_other_graph_is_not_blocked_by_a_held_graph_lock() -> None:
    rig = GatewayRig()
    gate = _Gate("held")
    rig.store.append_gate = gate
    held = _Submitter(rig, _group("held", "a"))
    other = _Submitter(rig, _group("other", "b"))

    held.start()
    assert gate.entered.wait(_WAIT_SECONDS)
    other.start()
    other_finished = other.finishes_within(_WAIT_SECONDS)
    held_finished_early = held.finishes_within(0)
    gate.release.set()
    assert held.finishes_within(_WAIT_SECONDS)

    assert other_finished
    assert not held_finished_early
    assert other.outcome is not None
    assert other.outcome.status == "applied"


def test_lock_is_released_when_a_group_is_rejected() -> None:
    rig = GatewayRig()
    rejected = MutationGroup(
        graph="compliance",
        audit_event_id=_AUDIT_EVENT_ID,
        primitives=(MergeProperty(label="Capability", id="ghost", properties={"a": 1}),),
    )

    def rejected_submit() -> None:
        try:
            rig.gateway.submit_group(rejected)
        except Exception:  # noqa: BLE001 -- only the lock's fate matters here
            return

    thread = threading.Thread(target=rejected_submit, daemon=True)
    thread.start()
    thread.join(_WAIT_SECONDS)
    follow_up = _Submitter(rig, _group("compliance", "a"))
    follow_up.start()

    assert follow_up.finishes_within(_WAIT_SECONDS)


def test_registry_hands_out_one_lock_per_graph_and_distinct_locks_across_graphs() -> None:
    registry = GraphLockRegistry()
    seen: list[object] = []
    sink = threading.Lock()

    def ask() -> None:
        lock = registry.lock_for("compliance")
        with sink:
            seen.append(lock)

    threads = [threading.Thread(target=ask) for _ in range(16)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(_WAIT_SECONDS)

    assert len({id(lock) for lock in seen}) == 1
    assert registry.lock_for("compliance") is not registry.lock_for("other")
