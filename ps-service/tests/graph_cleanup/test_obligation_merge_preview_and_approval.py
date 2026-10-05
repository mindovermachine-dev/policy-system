"""Preview and pair-bound approval for `merge-obligations`, plus `check-cleanup-approval`
for an obligation approval (issue #190, slice 11 sub-step d).

AC-BI-005 (preview with no writes + approval bound to the exact pair), AC-BI-015 (cross-role
rejected, no approval), AC-BI-016 (self / nonexistent rejected, no approval), AC-BI-017 (a
swapped pair yields a different signing challenge), A9 (reconciliation of an orphan row).
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

import pytest

from graph_cleanup._fakes import (
    OBL_ABSORBED,
    OBL_SURVIVOR,
    FakeApprovalStore,
    RecordingAuditStore,
    ScriptedObligationGraph,
    approval_row_count,
    expire_approval,
)
from ps_service.config import ServiceConfig
from ps_service.graph_cleanup.dependencies import GraphCleanupDependencies
from ps_service.graph_cleanup.errors import GraphCleanupValidationError
from ps_service.graph_cleanup.executors import TOOL_MERGE_OBLIGATIONS
from ps_service.graph_cleanup.service import (
    RECONCILE_GRACE,
    check_cleanup_approval,
    create_obligation_merge_approval,
    preview_obligation_merge,
)
from ps_service.logging import configure
from ps_service.passkey_signing.service import (
    _compute_sign_challenge,  # pyright: ignore[reportPrivateUsage]
)

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


def _create(
    graph: ScriptedObligationGraph,
    store: FakeApprovalStore,
    *,
    survivor_id: str = OBL_SURVIVOR,
    absorbed_id: str = OBL_ABSORBED,
):
    return create_obligation_merge_approval(
        graph,
        survivor_id=survivor_id,
        absorbed_id=absorbed_id,
        actor=_ACTOR,
        base_url="https://ps.example.com",
        store=store,
    )


def test_preview_names_edges_role_and_source_refs_and_writes_nothing() -> None:
    graph = ScriptedObligationGraph()

    preview = preview_obligation_merge(graph, survivor_id=OBL_SURVIVOR, absorbed_id=OBL_ABSORBED)

    assert preview.role_name == "Manufacturer"
    assert preview.edges_to_move.satisfied_by == ("req_1", "req_2")
    assert preview.edges_to_move.requires == ("cap_1",)
    assert preview.duplicate_edges_collapsed == 2
    assert [r.source_ref for r in preview.requirement_source_refs] == ["Art. 6(1)", "Art. 6(2)"]
    assert graph.write_calls == []


def test_approval_row_is_bound_to_the_exact_pair_and_state_digest() -> None:
    graph = ScriptedObligationGraph()
    store = FakeApprovalStore()

    approval = _create(graph, store)

    row = store.get_by_id(approval.pending_approval_id)
    assert row is not None
    assert row.tool_name == "merge-obligations"
    assert row.normalized_args == {
        "survivor_id": OBL_SURVIVOR,
        "absorbed_id": OBL_ABSORBED,
        "state_digest": approval.preview.state_digest,
    }
    assert (row.actor_subject, row.actor_issuer) == _ACTOR
    assert row.status == "pending"
    assert row.display_summary["absorbed_id"] == OBL_ABSORBED
    assert approval.approval_url.startswith(
        f"https://ps.example.com/approvals/{approval.pending_approval_id}#"
    )
    assert approval.expires_at == row.expires_at.isoformat()
    assert graph.write_calls == []


def test_a_swapped_pair_yields_a_different_signing_challenge() -> None:
    graph = ScriptedObligationGraph()
    store = FakeApprovalStore()
    first = _create(graph, store)
    second = _create(graph, store, survivor_id=OBL_ABSORBED, absorbed_id=OBL_SURVIVOR)

    rows = [store.get_by_id(a.pending_approval_id) for a in (first, second)]

    assert rows[0] is not None
    assert rows[1] is not None
    assert _compute_sign_challenge(rows[0]) != _compute_sign_challenge(rows[1])


def test_the_state_digest_is_part_of_what_is_signed() -> None:
    graph = ScriptedObligationGraph()
    store = FakeApprovalStore()
    first = _create(graph, store)
    graph.requires.append([OBL_ABSORBED, "cap_9"])
    second = _create(graph, store)

    assert first.preview.state_digest != second.preview.state_digest


@pytest.mark.parametrize(
    ("survivor_id", "absorbed_id", "fragment"),
    [
        (OBL_SURVIVOR, OBL_SURVIVOR, "itself"),
        (OBL_SURVIVOR, "obl_missing", "does not exist"),
        ("obl_missing", OBL_ABSORBED, "does not exist"),
    ],
)
def test_self_and_nonexistent_sides_are_rejected_with_no_approval(
    survivor_id: str, absorbed_id: str, fragment: str
) -> None:
    graph = ScriptedObligationGraph()
    store = FakeApprovalStore()

    with pytest.raises(GraphCleanupValidationError, match=fragment):
        _create(graph, store, survivor_id=survivor_id, absorbed_id=absorbed_id)

    assert approval_row_count(store) == 0
    assert graph.write_calls == []


def test_obligations_under_different_roles_are_rejected_with_no_approval() -> None:
    graph = ScriptedObligationGraph()
    graph.roles[1] = [OBL_ABSORBED, "role_2", "Importer"]
    store = FakeApprovalStore()

    with pytest.raises(GraphCleanupValidationError, match="different roles"):
        _create(graph, store)

    assert approval_row_count(store) == 0
    assert graph.write_calls == []


def _seed_signed(store: FakeApprovalStore, *, expired_for: timedelta) -> PendingApprovalRow:
    row, _code = store.create_pending_approval(
        tool_name=TOOL_MERGE_OBLIGATIONS,
        normalized_args={
            "survivor_id": OBL_SURVIVOR,
            "absorbed_id": OBL_ABSORBED,
            "state_digest": "d",
        },
        actor_subject=_ACTOR[0],
        actor_issuer=_ACTOR[1],
        display_summary={"role_id": "role_1"},
    )
    assert store.mark_signed(row.id)
    expire_approval(store, row.id, expired_for=expired_for)
    stored = store.get_by_id(row.id)
    assert stored is not None
    return stored


def _check(
    store: FakeApprovalStore,
    row: PendingApprovalRow,
    graph: ScriptedObligationGraph,
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


def test_check_reports_an_obligation_approval_to_its_creator() -> None:
    graph = ScriptedObligationGraph()
    store = FakeApprovalStore()
    approval = _create(graph, store)
    row = store.get_by_id(approval.pending_approval_id)
    assert row is not None

    status = _check(store, row, graph, RecordingAuditStore())

    assert status is not None
    assert status.tool_name == TOOL_MERGE_OBLIGATIONS
    assert status.status == "pending"


def test_an_orphan_obligation_row_with_the_effect_present_is_reconciled_as_applied() -> None:
    store = FakeApprovalStore()
    row = _seed_signed(store, expired_for=RECONCILE_GRACE + timedelta(seconds=5))
    graph = ScriptedObligationGraph(marker_count=1, absorbed_count=0)
    audit = RecordingAuditStore()

    status = _check(store, row, graph, audit)

    assert status is not None
    assert status.outcome == {"reconciled": "applied"}
    assert audit.rows == []


def test_an_orphan_obligation_row_without_effect_records_a_failed_row_then_a_failed_outcome() -> (
    None
):
    store = FakeApprovalStore()
    row = _seed_signed(store, expired_for=RECONCILE_GRACE + timedelta(seconds=5))
    graph = ScriptedObligationGraph(marker_count=0, absorbed_count=1)
    audit = RecordingAuditStore()

    status = _check(store, row, graph, audit)

    assert status is not None
    assert status.outcome is not None
    assert "error" in status.outcome
    [recorded] = audit.rows
    assert recorded.action == "obligation.merge"
    assert recorded.resource_type == "obligation"
    assert recorded.resource_id == OBL_ABSORBED
    assert recorded.outcome == "failed"
    assert recorded.details["approval_id"] == row.id
    assert recorded.details["reason_code"] == "interrupted_no_effect"
    assert recorded.details["role_id"] == "role_1"
