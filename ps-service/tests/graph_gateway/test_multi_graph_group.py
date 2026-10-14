"""A group over several graphs commits in one transaction.

Every test enters at `GraphWriteGateway.submit_multi_graph_group` against the Protocol fakes.
The fake store stages every leg on one transaction, so "one transaction" is observable as one
entry of `store.multi_appends`; a fault injected on one leg proves the others vanish with it.
"""

from __future__ import annotations

import json
import threading
from typing import TYPE_CHECKING

import pytest
import redis.exceptions
from pydantic import ValidationError

from graph_gateway._fakes import Fault, GatewayRig
from ps_service.graph_gateway.errors import (
    GraphApplyBlockedError,
    GraphApplyError,
    GraphLogPersistenceError,
    GraphLogUnavailableError,
    MissingTargetError,
    StaleGraphStateError,
    UnlistedNameError,
)
from ps_service.graph_gateway.graph_locks import GraphLockRegistry
from ps_service.graph_gateway.models import (
    ExpectedPosition,
    GraphLeg,
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
_WAIT_SECONDS = 5.0
_BLOCKED_PROBE_SECONDS = 0.3
_THREE = ("baseline", "native", "policy_system")


def _node(node_id: str) -> UpsertNode:
    return UpsertNode(label="Capability", id=node_id, properties={"name": node_id})


def _leg(graph: str, *primitives: Primitive, **fields: object) -> GraphLeg:
    return GraphLeg(graph=graph, primitives=primitives or (_node(f"{graph}-1"),), **fields)  # pyright: ignore[reportArgumentType]


def _set(*legs: GraphLeg, audit_event_id: str = _AUDIT_EVENT_ID) -> MultiGraphGroup:
    return MultiGraphGroup(audit_event_id=audit_event_id, legs=legs)


def _three_legs() -> tuple[GraphLeg, ...]:
    return (
        _leg("policy_system", _node("p-1"), _node("p-2")),
        _leg("native", _node("n-1")),
        _leg("baseline", _node("b-1"), _node("b-2"), _node("b-3")),
    )


def _outcome_by_graph(outcome: MultiGraphOutcome) -> dict[str, tuple[int | None, int | None, str]]:
    return {leg.graph: (leg.first_position, leg.last_position, leg.status) for leg in outcome.legs}


# -- the model -------------------------------------------------------------------------------


def test_a_set_needs_two_legs_each_on_a_different_graph_and_a_command_id() -> None:
    with pytest.raises(ValidationError):
        MultiGraphGroup(audit_event_id=_AUDIT_EVENT_ID, legs=(_leg("native"),))
    with pytest.raises(ValidationError):
        MultiGraphGroup(audit_event_id=_AUDIT_EVENT_ID, legs=(_leg("native"), _leg("native")))
    with pytest.raises(ValidationError):
        MultiGraphGroup(audit_event_id="not-a-uuid", legs=(_leg("native"), _leg("baseline")))


def test_a_leg_needs_at_least_one_primitive() -> None:
    with pytest.raises(ValidationError):
        GraphLeg(graph="native", primitives=())


# -- one transaction, per-graph outcome ------------------------------------------------------


def test_three_legs_commit_in_one_transaction_with_per_graph_positions_in_graph_order() -> None:
    rig = GatewayRig()

    outcome = rig.gateway.submit_multi_graph_group(_set(*_three_legs()))

    assert rig.store.multi_appends == [_THREE]  # one transaction, ascending
    assert [leg.graph for leg in outcome.legs] == list(_THREE)
    assert _outcome_by_graph(outcome) == {
        "baseline": (1, 3, "applied"),
        "native": (1, 1, "applied"),
        "policy_system": (1, 2, "applied"),
    }
    assert (outcome.status, outcome.audit_event_id) == ("applied", _AUDIT_EVENT_ID)
    assert set(rig.graphs.open("native").nodes) == {("Capability", "n-1")}
    assert {rig.store.read_applied_position(g) for g in ("native",)} == {1}
    assert rig.store.read_applied_position("baseline") == 3


def test_every_leg_group_carries_the_sets_audit_event_id() -> None:
    rig = GatewayRig()

    rig.gateway.submit_multi_graph_group(_set(*_three_legs()))

    groups = rig.store.read_groups_by_audit_event(_AUDIT_EVENT_ID)
    assert [group.graph for group in groups] == list(_THREE)
    assert [entry.identity for entry in groups[0].entries] == ["b-1", "b-2", "b-3"]


def test_the_locks_of_the_transaction_are_taken_in_ascending_graph_order() -> None:
    rig = GatewayRig()

    rig.gateway.submit_multi_graph_group(_set(*_three_legs()))  # legs given in descending order

    assert rig.store.lock_orders == [_THREE]


def test_a_leg_with_no_effective_change_is_not_logged_and_reports_unchanged() -> None:
    rig = GatewayRig()
    rig.gateway.submit_group(
        MutationGroup(graph="native", audit_event_id=_OTHER_EVENT_ID, primitives=(_node("n-1"),))
    )

    outcome = rig.gateway.submit_multi_graph_group(
        _set(_leg("native", _node("n-1")), _leg("baseline", _node("b-1")))
    )

    assert _outcome_by_graph(outcome) == {
        "baseline": (1, 1, "applied"),
        "native": (None, None, "unchanged"),
    }
    assert outcome.status == "applied"
    assert rig.store.multi_appends == [("baseline",)]
    assert rig.store.logged_count("native") == 1


def test_a_set_none_of_whose_legs_changes_anything_is_unchanged_and_logs_nothing() -> None:
    rig = GatewayRig()
    rig.gateway.submit_multi_graph_group(_set(_leg("native"), _leg("baseline")))
    attempts = rig.store.append_attempts

    outcome = rig.gateway.submit_multi_graph_group(_set(_leg("native"), _leg("baseline")))

    assert outcome.status == "unchanged"
    assert {leg.status for leg in outcome.legs} == {"unchanged"}
    assert rig.store.append_attempts == attempts


def test_a_set_logs_one_semantic_entry_and_one_apply_entry_per_leg_without_content(
    make_emitter: MakeEmitter, read_lines: ReadLines
) -> None:
    emitter, log_path = make_emitter()
    rig = GatewayRig(emitter)
    secret = "sentinel-secret-payload"
    legs = (
        _leg("baseline", UpsertNode(label="Capability", id="b", properties={"name": secret})),
        _leg("native", _node("n"), _node("n2")),
    )

    rig.gateway.submit_multi_graph_group(_set(*legs))
    emitter.flush()

    lines = read_lines(log_path)
    per_leg = [line for line in lines if line.get("action") == "apply_group"]
    assert [(line["graph"], line["audit_event_id"]) for line in per_leg] == [
        ("baseline", _AUDIT_EVENT_ID),
        ("native", _AUDIT_EVENT_ID),
    ]
    (whole,) = [line for line in lines if line.get("action") == "apply_multi_graph_group"]
    assert (whole["component"], whole["outcome"]) == ("graph_gateway", "success")
    assert (whole["audit_event_id"], whole["entry_count"]) == (_AUDIT_EVENT_ID, 3)
    assert secret not in json.dumps(lines)


def test_an_unchanged_set_and_a_pending_set_are_logged_with_their_outcome(
    make_emitter: MakeEmitter, read_lines: ReadLines
) -> None:
    emitter, log_path = make_emitter()
    rig = GatewayRig(emitter)
    rig.gateway.submit_multi_graph_group(_set(_leg("baseline"), _leg("native")))
    rig.gateway.submit_multi_graph_group(_set(_leg("baseline"), _leg("native")))
    rig.graphs.open("native").fail_on_write(redis.exceptions.ConnectionError("down"))
    rig.gateway.submit_multi_graph_group(
        _set(_leg("baseline", _node("b2")), _leg("native", _node("n2")))
    )
    emitter.flush()

    outcomes = [
        line["outcome"]
        for line in read_lines(log_path)
        if line.get("action") == "apply_multi_graph_group"
    ]
    assert outcomes == ["success", "unchanged", "pending"]


# -- whole-group rejection, nothing logged ----------------------------------------------------


def test_a_stale_position_on_any_leg_rejects_the_whole_set_and_logs_nothing() -> None:
    rig = GatewayRig()
    group = _set(
        _leg("baseline", preconditions=(ExpectedPosition(position=0),)),
        _leg("native", preconditions=(ExpectedPosition(position=4),)),
        _leg("policy_system"),
    )

    with pytest.raises(StaleGraphStateError):
        rig.gateway.submit_multi_graph_group(group)

    assert rig.store.entries == {}
    assert rig.store.append_attempts == 0
    assert all(not graph.nodes for graph in rig.graphs.graphs.values())


def test_an_unlisted_label_on_any_leg_rejects_the_whole_set() -> None:
    rig = GatewayRig()
    bad = UpsertNode(label="NotInTheSchema", id="x")

    with pytest.raises(UnlistedNameError):
        rig.gateway.submit_multi_graph_group(_set(_leg("baseline"), _leg("native", bad)))

    assert rig.store.entries == {}


def test_a_missing_edge_endpoint_on_a_late_leg_rejects_the_whole_set() -> None:
    rig = GatewayRig()
    dangling = UpsertEdge(
        type="HAS",
        identity="HAS:p-1->s-1",
        source=NodeRef(label="Policy", id="p-1"),
        target=NodeRef(label="Standard", id="s-1"),
    )

    with pytest.raises(MissingTargetError):
        rig.gateway.submit_multi_graph_group(_set(_leg("baseline"), _leg("native", dangling)))

    assert rig.store.entries == {}
    assert rig.store.append_attempts == 0


def test_a_blocked_graph_refuses_the_whole_set_before_anything_is_logged() -> None:
    rig = GatewayRig()
    rig.graphs.open("native").fail_on_write(redis.exceptions.ResponseError("refused"))
    with pytest.raises(GraphApplyError):  # a permanent refusal blocks the graph
        rig.gateway.submit_group(
            MutationGroup(graph="native", audit_event_id=_OTHER_EVENT_ID, primitives=(_node("x"),))
        )
    logged_before = rig.store.logged_count("baseline")

    with pytest.raises(GraphApplyBlockedError):
        rig.gateway.submit_multi_graph_group(_set(_leg("baseline"), _leg("native")))

    assert rig.store.logged_count("baseline") == logged_before == 0
    assert rig.store.multi_appends == []


# -- failure inside the transaction -----------------------------------------------------------


def test_an_append_failure_on_the_second_leg_leaves_no_leg_visible() -> None:
    rig = GatewayRig()
    rig.store.leg_append_faults["native"] = Fault(GraphLogPersistenceError("leg failed"), None)

    with pytest.raises(GraphLogPersistenceError):
        rig.gateway.submit_multi_graph_group(_set(*_three_legs()))

    assert rig.store.entries == {}
    assert rig.store.groups == []
    assert rig.store.lock_orders == [("baseline",)]  # leg 1 had staged and locked when leg 2 failed
    assert all(not graph.nodes for graph in rig.graphs.graphs.values())
    rig.store.leg_append_faults.clear()
    assert rig.gateway.submit_multi_graph_group(_set(*_three_legs())).status == "applied"


def test_a_log_outage_before_the_commit_is_retried_and_the_set_still_commits_once() -> None:
    rig = GatewayRig()
    rig.store.fail_appends(times=1)

    outcome = rig.gateway.submit_multi_graph_group(_set(_leg("baseline"), _leg("native")))

    assert outcome.status == "applied"
    assert rig.store.multi_appends == [("baseline", "native")]
    assert rig.sleeps  # one backoff happened


def test_a_log_outage_through_the_retry_budget_logs_nothing_and_raises_sanitized() -> None:
    rig = GatewayRig()
    rig.store.fail_appends()

    with pytest.raises(GraphLogUnavailableError):
        rig.gateway.submit_multi_graph_group(_set(_leg("baseline"), _leg("native")))

    assert rig.store.entries == {}


# -- apply: in place, leg by leg, pending per leg ---------------------------------------------


def test_a_graph_fault_on_one_leg_commits_the_set_and_reports_that_leg_pending() -> None:
    rig = GatewayRig()
    rig.graphs.open("native").fail_on_write(redis.exceptions.ConnectionError("down"))

    outcome = rig.gateway.submit_multi_graph_group(_set(*_three_legs()))

    statuses = {leg.graph: leg.status for leg in outcome.legs}
    assert statuses == {
        "baseline": "applied",
        "native": "committed_apply_pending",
        "policy_system": "applied",
    }
    assert outcome.status == "committed_apply_pending"
    assert rig.store.multi_appends == [_THREE]  # committed in one transaction all the same
    assert rig.store.read_applied_position("native") == 0
    assert rig.store.read_applied_position("baseline") == 3


def test_the_pending_leg_is_completed_by_catch_up() -> None:
    rig = GatewayRig()
    rig.graphs.open("native").fail_on_write(redis.exceptions.ConnectionError("down"))
    rig.gateway.submit_multi_graph_group(_set(*_three_legs()))
    rig.graphs.open("native").heal()

    result = rig.gateway.catch_up("native")

    assert result.caught_up
    assert set(rig.graphs.open("native").nodes) == {("Capability", "n-1")}


# -- concurrency: ascending order, no deadlock -------------------------------------


class _Gate:
    """Holds the first append that starts until released; later ones pass through."""

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


class _Thread:
    def __init__(self, work: object) -> None:
        self.result: object = None
        self.error: BaseException | None = None
        self._work = work
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        try:
            self.result = self._work()  # pyright: ignore[reportCallIssue, reportUnknownVariableType]
        except BaseException as exc:  # noqa: BLE001  (carried to the asserting thread)
            self.error = exc

    def start(self) -> _Thread:
        self._thread.start()
        return self

    def finishes_within(self, seconds: float) -> bool:
        self._thread.join(seconds)
        return not self._thread.is_alive()


def test_two_sets_naming_the_same_graphs_in_opposite_leg_order_finish_without_deadlock() -> None:
    rig = GatewayRig()
    gate = _Gate()
    rig.store.append_gate = gate
    first = _Thread(
        lambda: rig.gateway.submit_multi_graph_group(
            _set(_leg("baseline", _node("a")), _leg("native", _node("a")), _leg("zeta", _node("a")))
        )
    ).start()
    assert gate.entered.wait(_WAIT_SECONDS)
    second = _Thread(
        lambda: rig.gateway.submit_multi_graph_group(
            _set(
                _leg("zeta", _node("b")),
                _leg("native", _node("b")),
                _leg("baseline", _node("b")),
                audit_event_id=_OTHER_EVENT_ID,
            )
        )
    ).start()

    blocked = not second.finishes_within(_BLOCKED_PROBE_SECONDS)
    gate.release.set()

    assert blocked  # the second set waited for the first
    assert first.finishes_within(_WAIT_SECONDS)
    assert second.finishes_within(_WAIT_SECONDS)
    assert first.error is None
    assert second.error is None
    assert rig.store.lock_orders == [("baseline", "native", "zeta")] * 2
    assert [rig.store.logged_count(g) for g in ("baseline", "native", "zeta")] == [2, 2, 2]
    assert rig.store.read_applied_position("zeta") == 2


def test_a_single_graph_writer_on_a_shared_graph_serialises_with_a_set() -> None:
    rig = GatewayRig()
    gate = _Gate()
    rig.store.append_gate = gate
    single = _Thread(
        lambda: rig.gateway.submit_group(
            MutationGroup(
                graph="native", audit_event_id=_OTHER_EVENT_ID, primitives=(_node("single"),)
            )
        )
    ).start()
    assert gate.entered.wait(_WAIT_SECONDS)
    multi = _Thread(
        lambda: rig.gateway.submit_multi_graph_group(
            _set(_leg("baseline", _node("m")), _leg("native", _node("m")))
        )
    ).start()

    blocked = not multi.finishes_within(_BLOCKED_PROBE_SECONDS)
    gate.release.set()

    assert blocked
    assert single.finishes_within(_WAIT_SECONDS)
    assert multi.finishes_within(_WAIT_SECONDS)
    assert [e.identity for e in rig.store.entries["native"]] == ["single", "m"]
    assert rig.store.read_applied_position("native") == 2


def test_catch_up_of_one_leg_runs_alongside_a_set_that_holds_it_without_deadlock() -> None:
    """F-1 / A-1 (a): a single-lock catch-up and a multi-lock submit, opposite orders, a barrier."""
    rig = GatewayRig()
    gate = _Gate()
    rig.store.append_gate = gate
    submit = _Thread(lambda: rig.gateway.submit_multi_graph_group(_set(*_three_legs()))).start()
    assert gate.entered.wait(_WAIT_SECONDS)
    catch_up = _Thread(lambda: rig.gateway.catch_up("policy_system")).start()

    blocked = not catch_up.finishes_within(_BLOCKED_PROBE_SECONDS)
    gate.release.set()

    assert blocked
    assert submit.finishes_within(_WAIT_SECONDS)
    assert catch_up.finishes_within(_WAIT_SECONDS)
    assert catch_up.error is None
    assert rig.store.read_applied_position("policy_system") == 2


# -- lock_all ----------------------------------------------------------------------------------


def test_lock_all_takes_the_locks_in_ascending_order_dedupes_and_releases_in_full() -> None:
    registry = GraphLockRegistry()
    held_a = registry.lock_for("a")
    held_a.acquire()
    entered = threading.Event()

    def take() -> None:
        with registry.lock_all(["c", "b", "a", "b"]):
            entered.set()

    thread = threading.Thread(target=take, daemon=True)
    thread.start()
    thread.join(_BLOCKED_PROBE_SECONDS)

    # the thread waits for "a" first, so it holds neither "b" nor "c" while it waits
    assert thread.is_alive()
    assert registry.lock_for("b").acquire(blocking=False)
    registry.lock_for("b").release()
    assert registry.lock_for("c").acquire(blocking=False)
    registry.lock_for("c").release()

    held_a.release()
    thread.join(_WAIT_SECONDS)

    assert entered.is_set()
    assert not any(registry.lock_for(name).locked() for name in "abc")


def test_lock_all_releases_every_lock_when_its_body_raises() -> None:
    registry = GraphLockRegistry()

    with pytest.raises(RuntimeError), registry.lock_all(["b", "a"]):
        raise RuntimeError

    assert not registry.lock_for("a").locked()
    assert not registry.lock_for("b").locked()
