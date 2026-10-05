"""Preview and approval for `release-capability-governance`, plus `check-cleanup-approval`
for a release approval (issue #190, slice 14 d).

AC-BI-013 (a draft policy: preview, no writes, approval bound to the capability and policy),
AC-BI-014 / D12 (approved, deprecated and proposed rejected before any approval, with a pointer),
AC-BI-016-style validation (nonexistent, tombstone, ungoverned), A9 (reconciliation).
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

import pytest

from graph_cleanup._fakes import (
    SURVIVOR,
    FakeApprovalStore,
    RecordingAuditStore,
    ScriptedReleaseGraph,
    approval_row_count,
    expire_approval,
)
from ps_service.config import ServiceConfig
from ps_service.graph_cleanup.dependencies import GraphCleanupDependencies
from ps_service.graph_cleanup.errors import GraphCleanupValidationError
from ps_service.graph_cleanup.executors import TOOL_RELEASE_GOVERNANCE
from ps_service.graph_cleanup.service import (
    RECONCILE_GRACE,
    check_cleanup_approval,
    create_release_governance_approval,
    preview_release_governance,
)
from ps_service.logging import configure

if TYPE_CHECKING:
    from ps_service.passkey_signing.models import PendingApprovalRow

_ACTOR = ("officer", "https://issuer.example.com/")
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


def _create(graph: ScriptedReleaseGraph, store: FakeApprovalStore):
    return create_release_governance_approval(
        graph,
        capability_id=SURVIVOR,
        actor=_ACTOR,
        base_url="https://ps.example.com",
        store=store,
    )


def test_preview_names_the_capability_the_policy_and_changes_nothing() -> None:
    graph = ScriptedReleaseGraph()

    preview = preview_release_governance(graph, capability_id=SURVIVOR)

    assert preview.capability_id == SURVIVOR
    assert (preview.policy_id, preview.policy_title, preview.policy_status) == (
        "pol_1",
        "Incident Policy",
        "draft",
    )
    assert graph.release_calls == []


def test_a_draft_policy_creates_an_approval_bound_to_capability_policy_and_state() -> None:
    graph = ScriptedReleaseGraph()
    store = FakeApprovalStore()

    approval = _create(graph, store)

    row = store.get_by_id(approval.pending_approval_id)
    assert row is not None
    assert row.tool_name == "release-capability-governance"
    assert row.normalized_args == {
        "capability_id": SURVIVOR,
        "policy_id": "pol_1",
        "state_digest": approval.preview.state_digest,
    }
    assert (row.actor_subject, row.actor_issuer) == _ACTOR
    assert row.display_summary["policy_status"] == "draft"
    assert approval.approval_url.startswith(
        f"https://ps.example.com/approvals/{approval.pending_approval_id}#"
    )
    assert graph.release_calls == []


@pytest.mark.parametrize("status", ["approved", "deprecated", "proposed"])
def test_a_non_draft_policy_is_rejected_with_no_approval(status: str) -> None:
    graph = ScriptedReleaseGraph(governors=[[SURVIVOR, "pol_1", "Incident Policy", status]])
    store = FakeApprovalStore()

    with pytest.raises(GraphCleanupValidationError, match=status):
        _create(graph, store)

    assert approval_row_count(store) == 0
    assert graph.release_calls == []


def test_an_ungoverned_nonexistent_or_tombstoned_capability_creates_no_approval() -> None:
    store = FakeApprovalStore()
    cases = (
        ScriptedReleaseGraph(governors=[["cap_other", "pol_9", "X", "draft"]]),
        ScriptedReleaseGraph(nodes=[]),
    )
    for graph in cases:
        with pytest.raises(GraphCleanupValidationError):
            _create(graph, store)
    tombstoned = ScriptedReleaseGraph()
    tombstoned.nodes[0][2] = "merged"
    with pytest.raises(GraphCleanupValidationError, match="merged"):
        _create(tombstoned, store)

    assert approval_row_count(store) == 0


def _seed(store: FakeApprovalStore, *, expired_for: timedelta) -> PendingApprovalRow:
    row, _code = store.create_pending_approval(
        tool_name=TOOL_RELEASE_GOVERNANCE,
        normalized_args={"capability_id": SURVIVOR, "policy_id": "pol_1", "state_digest": "d"},
        actor_subject=_ACTOR[0],
        actor_issuer=_ACTOR[1],
        display_summary={},
    )
    assert store.mark_signed(row.id)
    expire_approval(store, row.id, expired_for=expired_for)
    stored = store.get_by_id(row.id)
    assert stored is not None
    return stored


def _check(
    store: FakeApprovalStore,
    row: PendingApprovalRow,
    graph: ScriptedReleaseGraph,
    audit: RecordingAuditStore,
):
    return check_cleanup_approval(
        pending_approval_id=row.id,
        actor=_ACTOR,
        store=store,
        config=_CONFIG,
        dependencies=GraphCleanupDependencies(
            open_single_tenant_graph=lambda _config: graph,
            audit_store=lambda _config: audit,
        ),
    )


def test_an_orphan_release_with_the_edge_gone_is_reconciled_as_applied() -> None:
    store = FakeApprovalStore()
    row = _seed(store, expired_for=RECONCILE_GRACE + timedelta(seconds=5))
    audit = RecordingAuditStore()

    status = _check(store, row, ScriptedReleaseGraph(edge_count=0), audit)

    assert status is not None
    assert status.tool_name == TOOL_RELEASE_GOVERNANCE
    assert status.outcome == {"reconciled": "applied"}
    assert audit.rows == []


def test_an_orphan_release_with_the_edge_still_present_records_a_failed_row() -> None:
    store = FakeApprovalStore()
    row = _seed(store, expired_for=RECONCILE_GRACE + timedelta(seconds=5))
    audit = RecordingAuditStore()

    status = _check(store, row, ScriptedReleaseGraph(edge_count=1), audit)

    assert status is not None
    assert status.outcome is not None
    assert "error" in status.outcome
    [recorded] = audit.rows
    assert recorded.action == "capability.release_governance"
    assert recorded.resource_type == "capability"
    assert recorded.resource_id == SURVIVOR
    assert recorded.outcome == "failed"
    assert recorded.details["approval_id"] == row.id
    assert recorded.details["policy_id"] == "pol_1"
    assert recorded.details["reason_code"] == "interrupted_no_effect"


def test_an_in_grace_release_row_is_left_untouched() -> None:
    store = FakeApprovalStore()
    row = _seed(store, expired_for=timedelta(seconds=5))

    status = _check(store, row, ScriptedReleaseGraph(edge_count=1), RecordingAuditStore())

    assert status is not None
    assert status.outcome is None
