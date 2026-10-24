"""`check_near_miss_merge_batch`: the creator's view of a batch and the reconciliation of one whose
signing was interrupted.

Over a `CleanupRig` a batch is created and marked signed. A recorded outcome is reported as is. A
signed batch with no outcome is `unsettled` per merge inside the grace window; past it, and once
the graph has caught up with its log, each merge is settled against the graph and the audit trail,
without ever running a merge again.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
import redis.exceptions

from graph_cleanup._fakes import FakeApprovalStore, expire_approval
from graph_cleanup._gateway_fakes import POLICY_SYSTEM
from graph_cleanup._merge_runs import CONFIG, OFFICER, roles
from graph_cleanup._near_miss_batch_worlds import (
    SeededPair,
    seed_capability_pair_with_review,
    seed_role_pair_with_review,
)
from ps_service.audit.errors import AuditPostgresUnavailableError
from ps_service.audit.models import AuditEventRow
from ps_service.graph_cleanup.near_miss_batch import create_near_miss_merge_batch
from ps_service.graph_cleanup.near_miss_batch_execution import execute_near_miss_batch
from ps_service.graph_cleanup.near_miss_batch_status import check_near_miss_merge_batch
from ps_service.graph_cleanup.service import RECONCILE_GRACE
from ps_service.logging import configure

if TYPE_CHECKING:
    from graph_cleanup._gateway_fakes import CleanupRig
    from ps_service.graph_cleanup.near_miss_batch_models import NearMissBatchStatus
    from ps_service.passkey_signing.models import PendingApprovalRow

_OTHER = ("someone-else", "https://issuer.example.com/")
_PAST_GRACE = RECONCILE_GRACE + timedelta(seconds=5)


@pytest.fixture(autouse=True)
def logging_configured() -> None:
    configure()


def _batch(
    rig: CleanupRig, store: FakeApprovalStore, review_ids: list[str], *, signed: bool = True
) -> PendingApprovalRow:
    approval = create_near_miss_merge_batch(
        rig.read,
        review_ids=review_ids,
        acknowledged_review_ids=[],
        actor=OFFICER,
        base_url="https://ps.example.com",
        store=store,
    )
    if signed:
        assert store.mark_signed(approval.pending_approval_id)
    row = store.get_by_id(approval.pending_approval_id)
    assert row is not None
    return row


def _check(
    rig: CleanupRig,
    store: FakeApprovalStore,
    approval_id: str,
    *,
    actor: tuple[str, str] | None = OFFICER,
) -> NearMissBatchStatus | None:
    return check_near_miss_merge_batch(
        pending_approval_id=approval_id,
        actor=actor,
        store=store,
        config=CONFIG,
        dependencies=rig.dependencies(roles()),
    )


def _states(status: NearMissBatchStatus) -> list[tuple[str, str]]:
    return [(merge.review_id, merge.state) for merge in status.merges]


def _orphan(
    rig: CleanupRig, store: FakeApprovalStore, pairs: list[SeededPair], *, applied: int
) -> PendingApprovalRow:
    """A signed batch with no outcome whose first `applied` merges did reach the graph."""
    row = _batch(rig, store, [pair.review_id for pair in pairs])
    if applied:
        prefix = FakeApprovalStore()
        executed = _batch(rig, prefix, [pair.review_id for pair in pairs[:applied]])
        execute_near_miss_batch(executed, CONFIG, rig.dependencies(roles()))
    expire_approval(store, row.id, expired_for=_PAST_GRACE)
    return row


def _stored(store: FakeApprovalStore, row: PendingApprovalRow) -> PendingApprovalRow:
    stored = store.get_by_id(row.id)
    assert stored is not None
    return stored


def _into_history(rig: CleanupRig) -> None:
    """Make the rows written so far visible to the audit query, as the real store would."""
    rig.audit.history[:0] = [
        AuditEventRow(
            id=f"event-{index}",
            occurred_at=datetime.now(UTC),
            actor_subject=r.actor_subject,
            actor_issuer=r.actor_issuer,
            action=r.action,
            resource_type=r.resource_type,
            resource_id=r.resource_id,
            outcome="failed" if r.outcome == "failed" else "applied",
            details=dict(r.details),
        )
        for index, r in enumerate(rig.audit.rows)
    ]


def test_a_pending_batch_reports_pending_with_no_entries(cleanup_rig: CleanupRig) -> None:
    store = FakeApprovalStore()
    pair = seed_capability_pair_with_review(cleanup_rig, 1)
    row = _batch(cleanup_rig, store, [pair.review_id], signed=False)

    status = _check(cleanup_rig, store, row.id)

    assert status is not None
    assert (status.status, status.merges) == ("pending", ())


def test_an_unsigned_batch_past_its_window_reports_expired(cleanup_rig: CleanupRig) -> None:
    store = FakeApprovalStore()
    pair = seed_capability_pair_with_review(cleanup_rig, 1)
    row = _batch(cleanup_rig, store, [pair.review_id], signed=False)
    expire_approval(store, row.id, expired_for=timedelta(seconds=1))

    status = _check(cleanup_rig, store, row.id)

    assert status is not None
    assert (status.status, status.merges) == ("expired", ())


def test_a_signed_batch_with_a_recorded_outcome_reports_one_entry_per_review_id(
    cleanup_rig: CleanupRig,
) -> None:
    store = FakeApprovalStore()
    capability = seed_capability_pair_with_review(cleanup_rig, 1)
    role = seed_role_pair_with_review(cleanup_rig, 1)
    row = _batch(cleanup_rig, store, [role.review_id, capability.review_id])
    store.set_outcome(
        row.id, execute_near_miss_batch(row, CONFIG, cleanup_rig.dependencies(roles()))
    )

    status = _check(cleanup_rig, store, row.id)

    assert status is not None
    assert status.status == "signed"
    assert [(m.review_id, m.state, m.winner_id, m.loser_id) for m in status.merges] == [
        (role.review_id, "merged", role.survivor_id, role.absorbed_id),
        (capability.review_id, "merged", capability.survivor_id, capability.absorbed_id),
    ]


def test_an_interrupted_batch_inside_the_grace_window_reports_signed_with_every_entry_unsettled(
    cleanup_rig: CleanupRig,
) -> None:
    store = FakeApprovalStore()
    pairs = [seed_capability_pair_with_review(cleanup_rig, n) for n in (1, 2)]
    row = _batch(cleanup_rig, store, [pair.review_id for pair in pairs])
    expire_approval(store, row.id, expired_for=timedelta(seconds=10))

    status = _check(cleanup_rig, store, row.id)

    assert status is not None
    assert status.status == "signed"
    assert _states(status) == [(pair.review_id, "unsettled") for pair in pairs]
    assert all(m.winner_id is None and m.error is None for m in status.merges)
    assert cleanup_rig.audit.rows == []
    assert _stored(store, row).outcome is None


def test_an_interrupted_batch_past_the_grace_window_settles_each_merge_and_runs_none(
    cleanup_rig: CleanupRig,
) -> None:
    store = FakeApprovalStore()
    pairs = [seed_capability_pair_with_review(cleanup_rig, n) for n in (1, 2, 3)]
    role = seed_role_pair_with_review(cleanup_rig, 1)
    row = _orphan(cleanup_rig, store, [*pairs, role], applied=1)
    groups_before = len(cleanup_rig.logged_groups)

    status = _check(cleanup_rig, store, row.id)

    assert status is not None
    assert status.status == "signed"
    assert _states(status) == [
        (pairs[0].review_id, "merged"),
        (pairs[1].review_id, "failed"),
        (pairs[2].review_id, "failed"),
        (role.review_id, "failed"),
    ]
    assert (status.merges[0].winner_id, status.merges[0].loser_id) == (
        pairs[0].survivor_id,
        pairs[0].absorbed_id,
    )
    assert status.merges[1].error
    assert len(cleanup_rig.logged_groups) == groups_before
    for pair in pairs[1:]:
        assert ("Capability", pair.absorbed_id) in cleanup_rig.graph.nodes
    stored = _stored(store, row)
    assert stored.outcome is not None
    assert (stored.outcome["merged_count"], stored.outcome["failed_count"]) == (1, 3)


def test_each_absent_item_gets_one_interrupted_failed_audit_row_and_a_repeat_check_adds_none(
    cleanup_rig: CleanupRig,
) -> None:
    store = FakeApprovalStore()
    pairs = [seed_capability_pair_with_review(cleanup_rig, n) for n in (1, 2)]
    role = seed_role_pair_with_review(cleanup_rig, 1)
    row = _orphan(cleanup_rig, store, [*pairs, role], applied=1)
    applied_rows = len(cleanup_rig.audit.rows)

    first = _check(cleanup_rig, store, row.id)
    second = _check(cleanup_rig, store, row.id)

    assert first is not None
    assert second is not None
    assert first.merges == second.merges
    new_rows = cleanup_rig.audit.rows[applied_rows:]
    assert [(r.action, r.outcome, r.resource_id) for r in new_rows] == [
        ("capability.merge", "failed", pairs[1].absorbed_id),
        ("role.merge", "failed", role.absorbed_id),
    ]
    assert {r.details["reason_code"] for r in new_rows} == {"interrupted_no_effect"}
    assert {r.details["approval_id"] for r in new_rows} == {row.id}
    assert [r.details["review_id"] for r in new_rows] == [pairs[1].review_id, role.review_id]


def test_an_item_that_already_has_a_failed_row_gets_no_second_one(
    cleanup_rig: CleanupRig,
) -> None:
    store = FakeApprovalStore()
    pairs = [seed_capability_pair_with_review(cleanup_rig, n) for n in (1, 2)]
    row = _orphan(cleanup_rig, store, pairs, applied=0)
    cleanup_rig.audit.history.append(
        AuditEventRow(
            id="earlier",
            occurred_at=datetime.now(UTC),
            actor_subject=OFFICER[0],
            actor_issuer=OFFICER[1],
            action="capability.merge",
            resource_type="capability",
            resource_id=pairs[0].absorbed_id,
            outcome="failed",
            details={"approval_id": row.id},
        )
    )

    status = _check(cleanup_rig, store, row.id)

    assert status is not None
    assert _states(status) == [(pair.review_id, "failed") for pair in pairs]
    assert [r.resource_id for r in cleanup_rig.audit.rows] == [pairs[1].absorbed_id]


def test_a_graph_behind_its_log_defers_the_reconciliation_and_leaves_the_row_untouched(
    cleanup_rig: CleanupRig,
) -> None:
    store = FakeApprovalStore()
    pairs = [seed_capability_pair_with_review(cleanup_rig, n) for n in (1, 2)]
    row = _orphan(cleanup_rig, store, pairs, applied=0)
    cleanup_rig.graph.fail_on_write(redis.exceptions.ConnectionError("down"))
    cleanup_rig.append_unrelated_group()
    assert not cleanup_rig.gateway.is_caught_up(POLICY_SYSTEM)

    status = _check(cleanup_rig, store, row.id)

    assert status is not None
    assert status.status == "signed"
    assert _states(status) == [(pair.review_id, "unsettled") for pair in pairs]
    assert cleanup_rig.audit.rows == []
    assert _stored(store, row).outcome is None


def test_a_failed_reconciliation_audit_write_leaves_the_outcome_unset_so_a_later_check_retries(
    cleanup_rig: CleanupRig,
) -> None:
    store = FakeApprovalStore()
    pairs = [seed_capability_pair_with_review(cleanup_rig, n) for n in (1, 2)]
    row = _orphan(cleanup_rig, store, pairs, applied=0)
    cleanup_rig.audit.raise_on_outcome["failed"] = AuditPostgresUnavailableError("down")

    with pytest.raises(AuditPostgresUnavailableError):
        _check(cleanup_rig, store, row.id)

    assert _stored(store, row).outcome is None
    cleanup_rig.audit.raise_on_outcome.clear()

    status = _check(cleanup_rig, store, row.id)

    assert status is not None
    assert _states(status) == [(pair.review_id, "failed") for pair in pairs]
    assert [r.resource_id for r in cleanup_rig.audit.rows] == [p.absorbed_id for p in pairs]


def test_a_retry_after_a_partial_audit_failure_records_each_failed_row_once(
    cleanup_rig: CleanupRig,
) -> None:
    store = FakeApprovalStore()
    pairs = [seed_capability_pair_with_review(cleanup_rig, n) for n in (1, 2)]
    row = _orphan(cleanup_rig, store, pairs, applied=0)
    audit = cleanup_rig.audit
    audit.raise_on_call[audit.standalone_calls + 2] = AuditPostgresUnavailableError("down")
    with pytest.raises(AuditPostgresUnavailableError):
        _check(cleanup_rig, store, row.id)
    audit.raise_on_call.clear()
    _into_history(cleanup_rig)

    status = _check(cleanup_rig, store, row.id)

    assert status is not None
    assert _states(status) == [(pair.review_id, "failed") for pair in pairs]
    assert [r.resource_id for r in audit.rows] == [p.absorbed_id for p in pairs]


def test_a_non_creator_and_a_non_batch_approval_are_not_found(cleanup_rig: CleanupRig) -> None:
    store = FakeApprovalStore()
    pair = seed_capability_pair_with_review(cleanup_rig, 1)
    row = _batch(cleanup_rig, store, [pair.review_id])
    other_tool, _code = store.create_pending_approval(
        tool_name="merge-capabilities",
        normalized_args={"survivor_id": "a", "absorbed_id": "b"},
        actor_subject=OFFICER[0],
        actor_issuer=OFFICER[1],
        display_summary={},
    )

    assert _check(cleanup_rig, store, row.id, actor=_OTHER) is None
    assert _check(cleanup_rig, store, row.id, actor=None) is None
    assert _check(cleanup_rig, store, "nope") is None
    assert _check(cleanup_rig, store, other_tool.id) is None
