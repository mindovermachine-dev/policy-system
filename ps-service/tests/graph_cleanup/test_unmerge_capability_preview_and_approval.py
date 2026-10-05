"""Preview and approval for capability `unmerge`, plus `check-cleanup-approval` (slice 15 d).

AC-BI-019 (preview lists restored edges and the survivor-added edges that stay; approval bound to
the merge and the state), AC-BI-020 (conflicts explained, no approval), A9 (reconciliation).
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
    ScriptedUnmergeGraph,
    approval_row_count,
    capability_merge_history,
    expire_approval,
)
from ps_service.config import ServiceConfig
from ps_service.graph_cleanup.dependencies import GraphCleanupDependencies
from ps_service.graph_cleanup.errors import GraphCleanupValidationError
from ps_service.graph_cleanup.executors import TOOL_UNMERGE
from ps_service.graph_cleanup.service import (
    RECONCILE_GRACE,
    check_cleanup_approval,
    create_unmerge_approval,
    preview_unmerge,
)
from ps_service.logging import configure

if TYPE_CHECKING:
    from collections.abc import Callable

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


def _audit() -> RecordingAuditStore:
    return RecordingAuditStore(history=capability_merge_history())


def _create(graph: ScriptedUnmergeGraph, store: FakeApprovalStore, audit: RecordingAuditStore):
    return create_unmerge_approval(
        graph,
        audit,
        merged_id=ABSORBED,
        actor=_ACTOR,
        base_url="https://ps.example.com",
        store=store,
    )


def test_preview_lists_the_edges_to_restore_and_the_survivor_added_edges_and_writes_nothing() -> (
    None
):
    graph = ScriptedUnmergeGraph()
    graph.requires = [*graph.requires, ["obl_new", SURVIVOR]]

    preview = preview_unmerge(graph, _audit(), merged_id=ABSORBED)

    assert preview.kind == "capability"
    assert (preview.merged_id, preview.survivor_id) == (ABSORBED, SURVIVOR)
    assert preview.merge_approval_id == "merge-approval-1"
    assert preview.edges_to_restore.requires == ("obl_1", "obl_2")
    assert preview.edges_removed_from_survivor.requires == ("obl_1",)
    assert [e.source_id for e in preview.survivor_added_edges] == ["obl_new"]
    assert graph.write_calls == []


def test_a_clean_unmerge_creates_an_approval_bound_to_the_merge_and_the_state() -> None:
    graph = ScriptedUnmergeGraph()
    store = FakeApprovalStore()

    approval = _create(graph, store, _audit())

    row = store.get_by_id(approval.pending_approval_id)
    assert row is not None
    assert row.tool_name == "unmerge"
    assert row.normalized_args == {
        "merged_id": ABSORBED,
        "merge_approval_id": "merge-approval-1",
        "kind": "capability",
        "state_digest": approval.preview.state_digest,
    }
    assert (row.actor_subject, row.actor_issuer) == _ACTOR
    assert row.display_summary["survivor_id"] == SURVIVOR
    assert approval.approval_url.startswith(
        f"https://ps.example.com/approvals/{approval.pending_approval_id}#"
    )
    assert graph.write_calls == []


def _survivor_merged_away(graph: ScriptedUnmergeGraph) -> None:
    graph.nodes[0][2] = "merged"
    graph.redirects = [[ABSORBED, SURVIVOR], [SURVIVOR, "cap_winner"]]


def _redirected_by_near_miss(graph: ScriptedUnmergeGraph) -> None:
    graph.redirects = [[ABSORBED, "cap_winner"]]


def _endpoint_gone(graph: ScriptedUnmergeGraph) -> None:
    graph.existing["Obligation"] = ["obl_2"]


def _already_unmerged(graph: ScriptedUnmergeGraph) -> None:
    graph.nodes[1][2] = "active"
    graph.redirects = []


@pytest.mark.parametrize(
    ("break_graph", "mention"),
    [
        (_survivor_merged_away, "cap_winner"),
        (_redirected_by_near_miss, "cap_winner"),
        (_endpoint_gone, "obl_1"),
        (_already_unmerged, "not a merged tombstone"),
    ],
)
def test_a_conflict_is_explained_and_creates_no_approval(
    break_graph: Callable[[ScriptedUnmergeGraph], None], mention: str
) -> None:
    graph = ScriptedUnmergeGraph()
    break_graph(graph)
    store = FakeApprovalStore()

    with pytest.raises(GraphCleanupValidationError, match=mention):
        _create(graph, store, _audit())

    assert approval_row_count(store) == 0
    assert graph.write_calls == []


def test_a_governance_change_conflict_is_explained_and_creates_no_approval() -> None:
    from graph_cleanup._fakes import capability_merge_history

    graph = ScriptedUnmergeGraph(governors=[[SURVIVOR, "pol_other", "Other", "approved"]])
    audit = RecordingAuditStore(history=capability_merge_history(absorbed_policy=("pol_1", "P")))
    store = FakeApprovalStore()

    with pytest.raises(GraphCleanupValidationError, match="governance"):
        _create(graph, store, audit)

    assert approval_row_count(store) == 0


def test_an_id_with_no_merge_in_the_audit_trail_creates_no_approval() -> None:
    store = FakeApprovalStore()

    with pytest.raises(GraphCleanupValidationError, match="no merge"):
        _create(ScriptedUnmergeGraph(), store, RecordingAuditStore())

    assert approval_row_count(store) == 0


# --- check-cleanup-approval for an unmerge approval (A9 reconciliation) -------------------------


def _seed(store: FakeApprovalStore, *, expired_for: timedelta) -> PendingApprovalRow:
    row, _code = store.create_pending_approval(
        tool_name=TOOL_UNMERGE,
        normalized_args={
            "merged_id": ABSORBED,
            "merge_approval_id": "merge-approval-1",
            "kind": "capability",
            "state_digest": "d",
        },
        actor_subject=_ACTOR[0],
        actor_issuer=_ACTOR[1],
        display_summary={"survivor_id": SURVIVOR},
    )
    assert store.mark_signed(row.id)
    expire_approval(store, row.id, expired_for=expired_for)
    stored = store.get_by_id(row.id)
    assert stored is not None
    return stored


def _check(
    store: FakeApprovalStore,
    row: PendingApprovalRow,
    graph: ScriptedUnmergeGraph,
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


def test_an_orphan_unmerge_with_the_node_active_again_is_reconciled_as_applied() -> None:
    store = FakeApprovalStore()
    row = _seed(store, expired_for=RECONCILE_GRACE + timedelta(seconds=5))
    audit = RecordingAuditStore()

    status = _check(store, row, ScriptedUnmergeGraph(unmerged_row=[1, 0]), audit)

    assert status is not None
    assert status.tool_name == TOOL_UNMERGE
    assert status.outcome == {"reconciled": "applied"}
    assert audit.rows == []


def test_an_orphan_unmerge_with_the_tombstone_still_present_records_a_failed_row() -> None:
    store = FakeApprovalStore()
    row = _seed(store, expired_for=RECONCILE_GRACE + timedelta(seconds=5))
    audit = RecordingAuditStore()

    status = _check(store, row, ScriptedUnmergeGraph(unmerged_row=[1, 1]), audit)

    assert status is not None
    assert status.outcome is not None
    assert "error" in status.outcome
    [recorded] = audit.rows
    assert recorded.action == "capability.unmerge"
    assert recorded.resource_type == "capability"
    assert recorded.resource_id == ABSORBED
    assert recorded.outcome == "failed"
    assert recorded.details["approval_id"] == row.id
    assert recorded.details["reverses_approval_id"] == "merge-approval-1"
    assert recorded.details["survivor_id"] == SURVIVOR
    assert recorded.details["reason_code"] == "interrupted_no_effect"


def test_an_in_grace_unmerge_row_is_left_untouched() -> None:
    store = FakeApprovalStore()
    row = _seed(store, expired_for=timedelta(seconds=5))

    status = _check(store, row, ScriptedUnmergeGraph(unmerged_row=[1, 1]), RecordingAuditStore())

    assert status is not None
    assert status.outcome is None
