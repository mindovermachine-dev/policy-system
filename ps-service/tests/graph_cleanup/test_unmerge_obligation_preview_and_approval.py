"""Preview and approval for obligation `unmerge`, plus `check-cleanup-approval` (slice 16 d).

AC-BI-019 (preview shows the node and edges to recreate and the survivor edges that stay,
approval bound to the merge and the state), AC-BI-020 (conflicts explained, no approval),
A9 (reconciliation).
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

import pytest

from graph_cleanup._fakes import (
    OBL_ABSORBED,
    OBL_SURVIVOR,
    ROLE_ID,
    FakeApprovalStore,
    RecordingAuditStore,
    ScriptedObligationUnmergeGraph,
    approval_row_count,
    expire_approval,
    obligation_merge_history,
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
    return RecordingAuditStore(history=obligation_merge_history())


def _create(
    graph: ScriptedObligationUnmergeGraph, store: FakeApprovalStore, audit: RecordingAuditStore
):
    return create_unmerge_approval(
        graph,
        audit,
        merged_id=OBL_ABSORBED,
        actor=_ACTOR,
        base_url="https://ps.example.com",
        store=store,
    )


def test_preview_shows_the_node_and_edges_to_recreate_and_the_survivor_edges_that_stay() -> None:
    graph = ScriptedObligationUnmergeGraph()

    preview = preview_unmerge(graph, _audit(), merged_id=OBL_ABSORBED)

    assert preview.kind == "obligation"
    assert (preview.merged_id, preview.survivor_id) == (OBL_ABSORBED, OBL_SURVIVOR)
    assert preview.merge_approval_id == "merge-approval-1"
    assert preview.merged_text == "Report  incidents."
    assert preview.edges_to_restore.satisfied_by == ("req_1", "req_2")
    assert preview.edges_to_restore.requires == ("cap_1",)
    assert [e.source_id for e in preview.survivor_edges_possibly_from_merge] == ["req_1"]
    assert graph.write_calls == []


def test_a_clean_unmerge_creates_an_approval_bound_to_the_merge_and_the_state() -> None:
    graph = ScriptedObligationUnmergeGraph()
    store = FakeApprovalStore()

    approval = _create(graph, store, _audit())

    row = store.get_by_id(approval.pending_approval_id)
    assert row is not None
    assert row.tool_name == "unmerge"
    assert row.normalized_args == {
        "merged_id": OBL_ABSORBED,
        "merge_approval_id": "merge-approval-1",
        "kind": "obligation",
        "state_digest": approval.preview.state_digest,
    }
    assert (row.actor_subject, row.actor_issuer) == _ACTOR
    assert row.display_summary["survivor_id"] == OBL_SURVIVOR
    assert row.display_summary["role_id"] == ROLE_ID
    assert graph.write_calls == []


def _exists_again(graph: ScriptedObligationUnmergeGraph) -> None:
    graph.nodes = [*graph.nodes, [OBL_ABSORBED, "again", 0.1]]


def _survivor_merged_away(graph: ScriptedObligationUnmergeGraph) -> None:
    graph.nodes = []
    graph.markers = [[OBL_ABSORBED, OBL_SURVIVOR], [OBL_SURVIVOR, "obl_winner"]]


def _no_marker(graph: ScriptedObligationUnmergeGraph) -> None:
    graph.markers = []


def _role_gone(graph: ScriptedObligationUnmergeGraph) -> None:
    graph.roles = []


def _requirement_gone(graph: ScriptedObligationUnmergeGraph) -> None:
    graph.requirements = [["req_2"]]


def _capability_tombstone(graph: ScriptedObligationUnmergeGraph) -> None:
    graph.capabilities = [["cap_1", "merged"]]


@pytest.mark.parametrize(
    ("break_graph", "mention"),
    [
        (_exists_again, "already exists"),
        (_survivor_merged_away, "obl_winner"),
        (_no_marker, "MergedObligation"),
        (_role_gone, ROLE_ID),
        (_requirement_gone, "req_1"),
        (_capability_tombstone, "cap_1"),
    ],
)
def test_a_conflict_is_explained_and_creates_no_approval(
    break_graph: Callable[[ScriptedObligationUnmergeGraph], None], mention: str
) -> None:
    graph = ScriptedObligationUnmergeGraph()
    break_graph(graph)
    store = FakeApprovalStore()

    with pytest.raises(GraphCleanupValidationError, match=mention):
        _create(graph, store, _audit())

    assert approval_row_count(store) == 0
    assert graph.write_calls == []


# --- check-cleanup-approval for an obligation unmerge approval (A9 reconciliation) ----------------


def _seed(store: FakeApprovalStore, *, expired_for: timedelta) -> PendingApprovalRow:
    row, _code = store.create_pending_approval(
        tool_name=TOOL_UNMERGE,
        normalized_args={
            "merged_id": OBL_ABSORBED,
            "merge_approval_id": "merge-approval-1",
            "kind": "obligation",
            "state_digest": "d",
        },
        actor_subject=_ACTOR[0],
        actor_issuer=_ACTOR[1],
        display_summary={"survivor_id": OBL_SURVIVOR, "role_id": ROLE_ID},
    )
    assert store.mark_signed(row.id)
    expire_approval(store, row.id, expired_for=expired_for)
    stored = store.get_by_id(row.id)
    assert stored is not None
    return stored


def _check(
    store: FakeApprovalStore,
    row: PendingApprovalRow,
    graph: ScriptedObligationUnmergeGraph,
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


def test_an_orphan_obligation_unmerge_with_the_node_back_is_reconciled_as_applied() -> None:
    store = FakeApprovalStore()
    row = _seed(store, expired_for=RECONCILE_GRACE + timedelta(seconds=5))
    audit = RecordingAuditStore()

    status = _check(
        store, row, ScriptedObligationUnmergeGraph(absorbed_count=1, marker_count=0), audit
    )

    assert status is not None
    assert status.outcome == {"reconciled": "applied"}
    assert audit.rows == []


def test_an_orphan_obligation_unmerge_with_the_marker_still_present_records_a_failed_row() -> None:
    store = FakeApprovalStore()
    row = _seed(store, expired_for=RECONCILE_GRACE + timedelta(seconds=5))
    audit = RecordingAuditStore()

    status = _check(
        store, row, ScriptedObligationUnmergeGraph(absorbed_count=0, marker_count=1), audit
    )

    assert status is not None
    assert status.outcome is not None
    assert "error" in status.outcome
    [recorded] = audit.rows
    assert recorded.action == "obligation.unmerge"
    assert recorded.resource_type == "obligation"
    assert recorded.resource_id == OBL_ABSORBED
    assert recorded.outcome == "failed"
    assert recorded.details["approval_id"] == row.id
    assert recorded.details["reverses_approval_id"] == "merge-approval-1"
    assert recorded.details["survivor_id"] == OBL_SURVIVOR
    assert recorded.details["role_id"] == ROLE_ID
    assert recorded.details["reason_code"] == "interrupted_no_effect"


def test_an_in_grace_obligation_unmerge_row_is_left_untouched() -> None:
    store = FakeApprovalStore()
    row = _seed(store, expired_for=timedelta(seconds=5))

    status = _check(
        store, row, ScriptedObligationUnmergeGraph(marker_count=1), RecordingAuditStore()
    )

    assert status is not None
    assert status.outcome is None
