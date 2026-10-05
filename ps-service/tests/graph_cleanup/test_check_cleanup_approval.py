"""`check-cleanup-approval` and the lazy reconciler (issue #190, slice 10 sub-step d; A9).

Creator-only visibility, live status, and reconciliation of a signed row whose outcome was
never recorded: effect present -> outcome reconciled; effect absent -> a `failed` audit row
(`interrupted_no_effect`, same approval id) and a failed outcome; idempotent; in-grace rows
are left alone.
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

import pytest

from graph_cleanup._fakes import (
    ABSORBED,
    SURVIVOR,
    FakeApprovalStore,
    RecordingAuditStore,
    ScriptedMergeGraph,
    expire_approval,
)
from ps_service.audit.errors import AuditPostgresUnavailableError
from ps_service.config import ServiceConfig
from ps_service.graph_cleanup.dependencies import GraphCleanupDependencies
from ps_service.graph_cleanup.errors import GraphCleanupPersistenceError
from ps_service.graph_cleanup.executors import TOOL_MERGE_CAPABILITIES
from ps_service.graph_cleanup.service import RECONCILE_GRACE, check_cleanup_approval
from ps_service.logging import configure

if TYPE_CHECKING:
    from ps_service.passkey_signing.models import PendingApprovalRow

_ACTOR = ("officer", "https://issuer.example.com/")
_OTHER = ("someone-else", "https://issuer.example.com/")
_CONFIG = ServiceConfig(
    host="127.0.0.1",
    port=8000,
    graceful_shutdown_seconds=10,
    logging_dir=None,
    is_local_test_bypass_active=False,
)


@pytest.fixture(autouse=True)
def logging_configured() -> None:
    configure()


def _seed(
    store: FakeApprovalStore,
    *,
    tool_name: str = TOOL_MERGE_CAPABILITIES,
    signed: bool = False,
    outcome: dict[str, object] | None = None,
    expired_for: timedelta | None = None,
    display_summary: dict[str, object] | None = None,
) -> PendingApprovalRow:
    row, _code = store.create_pending_approval(
        tool_name=tool_name,
        normalized_args={
            "survivor_id": SURVIVOR,
            "absorbed_id": ABSORBED,
            "acknowledge_governance_change": False,
            "state_digest": "d",
        },
        actor_subject=_ACTOR[0],
        actor_issuer=_ACTOR[1],
        display_summary=display_summary or {},
    )
    if signed:
        assert store.mark_signed(row.id)
    if outcome is not None:
        store.set_outcome(row.id, outcome)
    if expired_for is not None:
        expire_approval(store, row.id, expired_for=expired_for)
    stored = store.get_by_id(row.id)
    assert stored is not None
    return stored


def _check(
    store: FakeApprovalStore,
    row: PendingApprovalRow,
    *,
    graph: ScriptedMergeGraph | None = None,
    audit: RecordingAuditStore | None = None,
    actor: tuple[str, str] | None = _ACTOR,
):
    scripted = graph if graph is not None else ScriptedMergeGraph()
    recorder = audit if audit is not None else RecordingAuditStore()
    return check_cleanup_approval(
        pending_approval_id=row.id,
        actor=actor,
        store=store,
        config=_CONFIG,
        dependencies=GraphCleanupDependencies(
            open_single_tenant_graph=lambda _config: scripted,
            audit_store=lambda _config: recorder,
        ),
    )


def test_a_pending_approval_reports_pending_to_its_creator() -> None:
    store = FakeApprovalStore()
    row = _seed(store)

    status = _check(store, row)

    assert status is not None
    assert status.status == "pending"
    assert status.tool_name == TOOL_MERGE_CAPABILITIES
    assert status.outcome is None


def test_another_actor_or_no_actor_gets_nothing() -> None:
    store = FakeApprovalStore()
    row = _seed(store)

    assert _check(store, row, actor=_OTHER) is None
    assert _check(store, row, actor=None) is None


def test_an_unknown_id_and_a_near_miss_row_are_not_found() -> None:
    store = FakeApprovalStore()
    near_miss = _seed(store, tool_name="near_misses_resolve")

    assert _check(store, near_miss) is None
    assert (
        check_cleanup_approval(
            pending_approval_id="nope",
            actor=_ACTOR,
            store=store,
            config=_CONFIG,
            dependencies=GraphCleanupDependencies(
                open_single_tenant_graph=lambda _c: ScriptedMergeGraph()
            ),
        )
        is None
    )


def test_an_unsigned_row_past_its_window_reports_expired() -> None:
    store = FakeApprovalStore()
    row = _seed(store, expired_for=timedelta(seconds=1))

    status = _check(store, row)

    assert status is not None
    assert status.status == "expired"


def test_a_signed_row_with_an_outcome_reports_it() -> None:
    store = FakeApprovalStore()
    row = _seed(store, signed=True, outcome={"merged": True, "survivor_id": SURVIVOR})

    status = _check(store, row)

    assert status is not None
    assert status.status == "signed"
    assert status.outcome == {"merged": True, "survivor_id": SURVIVOR}


def test_a_signed_row_inside_the_grace_window_is_left_untouched() -> None:
    store = FakeApprovalStore()
    row = _seed(store, signed=True, expired_for=timedelta(seconds=10))
    graph = ScriptedMergeGraph(tombstone_count=1)
    audit = RecordingAuditStore()

    status = _check(store, row, graph=graph, audit=audit)

    assert status is not None
    assert status.outcome is None
    assert graph.queries == []
    assert audit.rows == []


def test_an_orphan_signed_row_whose_effect_is_present_is_reconciled_as_applied() -> None:
    store = FakeApprovalStore()
    row = _seed(store, signed=True, expired_for=RECONCILE_GRACE + timedelta(seconds=5))
    graph = ScriptedMergeGraph(tombstone_count=1)
    audit = RecordingAuditStore()

    status = _check(store, row, graph=graph, audit=audit)

    assert status is not None
    assert status.outcome == {"reconciled": "applied"}
    assert audit.rows == []
    stored = store.get_by_id(row.id)
    assert stored is not None
    assert stored.outcome == {"reconciled": "applied"}


def test_an_orphan_signed_row_without_effect_records_a_failed_row_then_a_failed_outcome() -> None:
    store = FakeApprovalStore()
    row = _seed(store, signed=True, expired_for=RECONCILE_GRACE + timedelta(seconds=5))
    graph = ScriptedMergeGraph(tombstone_count=0)
    audit = RecordingAuditStore()

    status = _check(store, row, graph=graph, audit=audit)

    assert status is not None
    assert status.outcome is not None
    assert "error" in status.outcome
    [recorded] = audit.rows
    assert recorded.outcome == "failed"
    assert recorded.action == "capability.merge"
    assert recorded.resource_id == ABSORBED
    assert recorded.details["approval_id"] == row.id
    assert recorded.details["reason_code"] == "interrupted_no_effect"


def test_repeated_checks_are_idempotent() -> None:
    store = FakeApprovalStore()
    row = _seed(store, signed=True, expired_for=RECONCILE_GRACE + timedelta(seconds=5))
    graph = ScriptedMergeGraph(tombstone_count=0)
    audit = RecordingAuditStore()

    first = _check(store, row, graph=graph, audit=audit)
    second = _check(store, row, graph=graph, audit=audit)

    assert first is not None
    assert second is not None
    assert first.outcome == second.outcome
    assert len(audit.rows) == 1


def test_a_failed_reconciliation_audit_write_leaves_the_outcome_unset_so_it_can_retry() -> None:
    store = FakeApprovalStore()
    row = _seed(store, signed=True, expired_for=RECONCILE_GRACE + timedelta(seconds=5))
    audit = RecordingAuditStore()
    audit.raise_on_outcome["failed"] = AuditPostgresUnavailableError("down")

    with pytest.raises(AuditPostgresUnavailableError):
        _check(store, row, graph=ScriptedMergeGraph(tombstone_count=0), audit=audit)

    stored = store.get_by_id(row.id)
    assert stored is not None
    assert stored.outcome is None


def test_an_unreadable_graph_during_reconciliation_leaves_the_row_untouched() -> None:
    import redis.exceptions

    store = FakeApprovalStore()
    row = _seed(store, signed=True, expired_for=RECONCILE_GRACE + timedelta(seconds=5))
    graph = ScriptedMergeGraph(read_error=redis.exceptions.ConnectionError("down"))

    with pytest.raises(GraphCleanupPersistenceError):
        _check(store, row, graph=graph)

    stored = store.get_by_id(row.id)
    assert stored is not None
    assert stored.outcome is None


def test_the_grace_period_is_five_minutes() -> None:
    assert timedelta(seconds=300) == RECONCILE_GRACE


def test_an_orphan_case_two_row_without_effect_keeps_the_policy_case_in_its_failed_row() -> None:
    store = FakeApprovalStore()
    row = _seed(
        store,
        signed=True,
        expired_for=RECONCILE_GRACE + timedelta(seconds=5),
        display_summary={"policy_case": 2},
    )
    audit = RecordingAuditStore()

    _check(store, row, graph=ScriptedMergeGraph(tombstone_count=0), audit=audit)

    [recorded] = audit.rows
    assert recorded.details["policy_case"] == 2
    assert recorded.details["reason_code"] == "interrupted_no_effect"


def test_an_orphan_case_three_row_without_effect_keeps_the_policy_case_in_its_failed_row() -> None:
    store = FakeApprovalStore()
    row = _seed(
        store,
        signed=True,
        expired_for=RECONCILE_GRACE + timedelta(seconds=5),
        display_summary={"policy_case": 3},
    )
    audit = RecordingAuditStore()

    _check(store, row, graph=ScriptedMergeGraph(tombstone_count=0), audit=audit)

    [recorded] = audit.rows
    assert recorded.details["policy_case"] == 3
