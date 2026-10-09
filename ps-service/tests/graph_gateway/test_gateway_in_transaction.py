"""Submitting a group on the caller's own transaction (issue #206 S7a, App-C).

A writer that records an audit row and the log group on ONE transaction calls
`submit_group_in_transaction(cur, group)`. The call stages the group (graph lock taken, state
read, append on the caller's cursor); the caller commits, then `staged.complete()` applies the
entries and returns the outcome. The graph lock is held until `complete`, `abort` or leaving the
`with` block. The log store is the in-memory fake with a shared-transaction capability that keeps
the real `append_group(cur, group, audit_event_id=...)` signature and lock-until-commit behavior.
"""

from __future__ import annotations

import threading

import pytest

from graph_gateway._fakes import GatewayRig, InMemoryTransaction
from ps_service.graph_gateway.errors import (
    GraphLogPersistenceError,
    MissingTargetError,
    StagedGroupNotCommittedError,
    StagedSubmissionClosedError,
)
from ps_service.graph_gateway.models import (
    GroupOutcome,
    MergeProperty,
    MutationGroup,
    UpsertNode,
)

_GRAPH = "compliance"
_WAIT_SECONDS = 5.0
_BLOCKED_PROBE_SECONDS = 0.3


def _group(audit_event_id: str, node_id: str = "cap-1", graph: str = _GRAPH) -> MutationGroup:
    return MutationGroup(
        graph=graph,
        audit_event_id=audit_event_id,
        primitives=(UpsertNode(label="Capability", id=node_id, properties={"name": node_id}),),
    )


def _threaded_submit(
    rig: GatewayRig, group: MutationGroup
) -> tuple[threading.Thread, list[GroupOutcome]]:
    outcomes: list[GroupOutcome] = []

    def run() -> None:
        outcomes.append(rig.gateway.submit_group(group))

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, outcomes


def _new_audit(rig: GatewayRig) -> str:
    """Commit an audit row of its own, as a standalone submit's audit link needs."""
    transaction = rig.store.begin()
    audit_event_id = transaction.record_audit()
    transaction.commit()
    return audit_event_id


def _tx(rig: GatewayRig) -> tuple[InMemoryTransaction, str]:
    transaction = rig.store.begin()
    return transaction, transaction.record_audit()


def test_in_transaction_submit_links_audit_row_and_group_atomically() -> None:
    rig = GatewayRig()
    graph = rig.graphs.open(_GRAPH)

    rolled_back, rolled_back_audit = _tx(rig)
    with rig.gateway.submit_group_in_transaction(
        rolled_back.cursor, _group(rolled_back_audit)
    ) as staged:
        assert graph.nodes == {}  # staged, neither committed nor applied
        rolled_back.rollback()
        staged.abort()
    assert rig.store.audit_rows == set()
    assert rig.store.entries == {}
    assert rig.store.groups == []
    assert graph.nodes == {}
    assert rig.store.read_applied_position(_GRAPH) == 0

    committed, committed_audit = _tx(rig)
    with rig.gateway.submit_group_in_transaction(
        committed.cursor, _group(committed_audit)
    ) as staged:
        committed.commit()
        outcome = staged.complete()
    assert rig.store.audit_rows == {committed_audit}
    (logged,) = rig.store.read_groups_by_audit_event(committed_audit)
    assert (logged.graph, logged.first_position, logged.last_position) == (_GRAPH, 1, 1)
    assert outcome.status == "applied"
    assert set(graph.nodes) == {("Capability", "cap-1")}


def test_complete_applies_after_caller_commit_and_returns_outcome() -> None:
    rig = GatewayRig()
    transaction, audit_event_id = _tx(rig)

    with rig.gateway.submit_group_in_transaction(
        transaction.cursor, _group(audit_event_id)
    ) as staged:
        assert staged.status == "staged"
        transaction.commit()
        assert rig.graphs.open(_GRAPH).nodes == {}  # committed but not yet applied
        assert rig.store.read_applied_position(_GRAPH) == 0
        outcome = staged.complete()

    assert (outcome.graph, outcome.first_position, outcome.last_position, outcome.status) == (
        _GRAPH,
        1,
        1,
        "applied",
    )
    assert set(rig.graphs.open(_GRAPH).nodes) == {("Capability", "cap-1")}
    assert rig.store.read_applied_position(_GRAPH) == rig.store.last_position(_GRAPH) == 1
    assert rig.events == ["graph_read", "log_append", "graph_write"]


@pytest.mark.parametrize("ending", ["complete", "abort"])
def test_in_transaction_submission_holds_graph_lock_until_complete_or_abort(ending: str) -> None:
    rig = GatewayRig()
    transaction, audit_event_id = _tx(rig)
    staged = rig.gateway.submit_group_in_transaction(transaction.cursor, _group(audit_event_id))
    blocked_thread, blocked_outcomes = _threaded_submit(rig, _group(_new_audit(rig), "cap-2"))
    other_graph_thread, other_graph_outcomes = _threaded_submit(
        rig, _group(_new_audit(rig), "cap-3", graph="other")
    )

    other_graph_thread.join(_WAIT_SECONDS)
    blocked_thread.join(_BLOCKED_PROBE_SECONDS)

    assert not other_graph_thread.is_alive()
    assert other_graph_outcomes[0].status == "applied"
    assert blocked_thread.is_alive()  # same graph: held back by the staged submission

    if ending == "complete":
        transaction.commit()
        staged.complete()
        first_position = 2
    else:
        transaction.rollback()
        staged.abort()
        first_position = 1
    blocked_thread.join(_WAIT_SECONDS)

    assert not blocked_thread.is_alive()
    assert blocked_outcomes[0].first_position == first_position


def _fail_inside_the_block(
    rig: GatewayRig, transaction: InMemoryTransaction, audit_event_id: str
) -> None:
    with rig.gateway.submit_group_in_transaction(transaction.cursor, _group(audit_event_id)):
        transaction.rollback()
        message = "caller failed"
        raise RuntimeError(message)


def test_in_transaction_context_manager_releases_lock_on_exception() -> None:
    rig = GatewayRig()
    transaction, audit_event_id = _tx(rig)

    with pytest.raises(RuntimeError, match="caller failed"):
        _fail_inside_the_block(rig, transaction, audit_event_id)

    follow_up, outcomes = _threaded_submit(rig, _group(_new_audit(rig), "cap-2"))
    follow_up.join(_WAIT_SECONDS)
    assert not follow_up.is_alive()
    assert outcomes[0].first_position == 1  # nothing of the failed submission remained
    assert set(rig.graphs.open(_GRAPH).nodes) == {("Capability", "cap-2")}


def test_leaving_the_block_without_complete_does_not_apply_but_the_next_write_catches_up() -> None:
    rig = GatewayRig()
    transaction, audit_event_id = _tx(rig)

    with rig.gateway.submit_group_in_transaction(transaction.cursor, _group(audit_event_id)):
        transaction.commit()

    assert rig.graphs.open(_GRAPH).nodes == {}
    assert (rig.store.last_position(_GRAPH), rig.store.read_applied_position(_GRAPH)) == (1, 0)

    outcome = rig.gateway.submit_group(_group(_new_audit(rig), "cap-2"))

    assert outcome.first_position == 2
    assert set(rig.graphs.open(_GRAPH).nodes) == {("Capability", "cap-1"), ("Capability", "cap-2")}


def test_in_transaction_all_noop_group_is_unchanged_appends_nothing_and_holds_no_lock() -> None:
    rig = GatewayRig()
    rig.gateway.submit_group(_group(_new_audit(rig)))
    groups_before = len(rig.store.groups)
    transaction, audit_event_id = _tx(rig)

    staged = rig.gateway.submit_group_in_transaction(transaction.cursor, _group(audit_event_id))
    follow_up, outcomes = _threaded_submit(rig, _group(_new_audit(rig), "cap-2"))
    follow_up.join(_WAIT_SECONDS)
    transaction.commit()
    outcome = staged.complete()

    assert staged.status == "unchanged"
    assert outcome.status == "unchanged"
    assert (outcome.first_position, outcome.last_position) == (None, None)
    assert len(rig.store.groups) == groups_before + 1  # only the follow-up's group was added
    assert not follow_up.is_alive()  # the no-op never kept the lock
    assert outcomes[0].first_position == 2


def test_in_transaction_rejection_releases_the_lock_and_raises() -> None:
    rig = GatewayRig()
    transaction, audit_event_id = _tx(rig)
    rejected = MutationGroup(
        graph=_GRAPH,
        audit_event_id=audit_event_id,
        primitives=(MergeProperty(label="Capability", id="ghost", properties={"a": 1}),),
    )

    with pytest.raises(MissingTargetError):
        rig.gateway.submit_group_in_transaction(transaction.cursor, rejected)

    follow_up, _ = _threaded_submit(rig, _group(_new_audit(rig), "cap-2"))
    follow_up.join(_WAIT_SECONDS)
    assert not follow_up.is_alive()


def test_in_transaction_append_failure_releases_the_lock_and_raises() -> None:
    rig = GatewayRig()
    transaction = rig.store.begin()
    unrecorded_audit_event = "3f2b8c1e-5d4a-4b7e-9a61-0c2d7e8f9a10"

    with pytest.raises(GraphLogPersistenceError):
        rig.gateway.submit_group_in_transaction(transaction.cursor, _group(unrecorded_audit_event))
    transaction.rollback()

    follow_up, _ = _threaded_submit(rig, _group(_new_audit(rig), "cap-2"))
    follow_up.join(_WAIT_SECONDS)
    assert not follow_up.is_alive()


def test_complete_before_the_caller_committed_raises_and_releases_the_lock() -> None:
    rig = GatewayRig()
    transaction, audit_event_id = _tx(rig)
    staged = rig.gateway.submit_group_in_transaction(transaction.cursor, _group(audit_event_id))

    with pytest.raises(StagedGroupNotCommittedError):
        staged.complete()

    transaction.rollback()
    assert rig.graphs.open(_GRAPH).nodes == {}
    follow_up, _ = _threaded_submit(rig, _group(_new_audit(rig), "cap-2"))
    follow_up.join(_WAIT_SECONDS)
    assert not follow_up.is_alive()


def test_a_closed_submission_cannot_be_completed_again() -> None:
    rig = GatewayRig()
    transaction, audit_event_id = _tx(rig)
    staged = rig.gateway.submit_group_in_transaction(transaction.cursor, _group(audit_event_id))
    transaction.commit()
    staged.complete()

    with pytest.raises(StagedSubmissionClosedError):
        staged.complete()
    staged.abort()  # releasing twice is harmless
