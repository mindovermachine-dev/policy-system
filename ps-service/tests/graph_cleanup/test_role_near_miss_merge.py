"""Merging a `Role` near miss as a batch of one goes through the Role merge executor.

`create_near_miss_merge_batch` reads the review's two sides under its own kind, picks the survivor,
and creates one approval with the review id signed into it. The tests drive it over a `CleanupRig`
(the real reader, planner and executor over an in-memory graph and gateway), sign the approval the
way the router does, and look at the graph afterwards.

A Role node carries no `created_at`, so the survivor of a Role review is always the incoming Role
(the node whose arrival raised the near miss); one test pins that rule.
"""

from __future__ import annotations

import copy
from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast

import pytest
from authz._fakes import FakeAccessRoleStore

from graph_cleanup._fakes import (
    ROLE_ABSORBED,
    ROLE_SURVIVOR,
    FakeApprovalStore,
    approval_row_count,
)
from graph_cleanup._merge_runs import CONFIG, OFFICER, OWNER, roles
from ps_service.audit.models import AuditEventRow
from ps_service.graph_cleanup.executors import execute_unmerge
from ps_service.graph_cleanup.models import RoleMergePreview
from ps_service.graph_cleanup.near_miss_batch import create_near_miss_merge_batch
from ps_service.graph_cleanup.near_miss_batch_execution import execute_near_miss_batch
from ps_service.graph_cleanup.near_miss_batch_models import (
    TOOL_MERGE_NEAR_MISSES,
    NearMissBatchApproval,
    NearMissBatchRefusedError,
)
from ps_service.graph_cleanup.near_miss_batch_status import check_near_miss_merge_batch
from ps_service.graph_cleanup.service import create_unmerge_approval
from ps_service.logging import configure

if TYPE_CHECKING:
    from graph_cleanup._gateway_fakes import CleanupRig
    from ps_service.passkey_signing.models import PendingApprovalRow

_REVIEW = "review_role_aaa"
_BASE_URL = "https://ps.example.com"


def _world(
    rig: CleanupRig, *, incoming: str = ROLE_SURVIVOR, existing: str = ROLE_ABSORBED
) -> None:
    """Two Roles of one instrument and a `Role` review; the incoming Role wins by default."""
    rig.seed_role_merge_world()
    rig.seed_near_miss_review(_REVIEW, incoming_id=incoming, existing_id=existing, kind="Role")


@pytest.fixture(autouse=True)
def logging_configured() -> None:
    configure()


def _request(
    rig: CleanupRig, store: FakeApprovalStore, *, acknowledge: bool = False
) -> NearMissBatchApproval:
    return create_near_miss_merge_batch(
        rig.read,
        review_ids=[_REVIEW],
        acknowledged_review_ids=[_REVIEW] if acknowledge else [],
        actor=OFFICER,
        base_url=_BASE_URL,
        store=store,
    )


def _sign(store: FakeApprovalStore, approval_id: str) -> PendingApprovalRow:
    assert store.mark_signed(approval_id)
    row = store.get_by_id(approval_id)
    assert row is not None
    return row


def _graph_state(rig: CleanupRig) -> tuple[object, object]:
    return copy.deepcopy(rig.graph.nodes), copy.deepcopy(rig.graph.edges)


def _merges(outcome: dict[str, object]) -> list[dict[str, object]]:
    return cast("list[dict[str, object]]", outcome["merges"])


def _has_sources(rig: CleanupRig, obligation_id: str) -> list[str]:
    return [key[2] for key in rig.graph.edges if key[0] == "HAS" and key[4] == obligation_id]


# -- the request ---------------------------------------------------------------------------------


def test_a_role_review_yields_a_batch_approval_with_the_review_signed_in(
    cleanup_rig: CleanupRig,
) -> None:
    rig = cleanup_rig
    _world(rig)
    store = FakeApprovalStore()
    before = _graph_state(rig)

    approval = _request(rig, store)

    preview = approval.merges[0].preview
    assert isinstance(preview, RoleMergePreview)
    assert (preview.survivor_id, preview.absorbed_id) == (ROLE_SURVIVOR, ROLE_ABSORBED)
    row = store.get_by_id(approval.pending_approval_id)
    assert row is not None
    assert row.tool_name == TOOL_MERGE_NEAR_MISSES
    [signed] = cast("list[dict[str, object]]", row.normalized_args["merges"])
    assert signed["review_id"] == _REVIEW
    assert signed["kind"] == "Role"
    assert approval_row_count(store) == 1
    assert _graph_state(rig) == before


def test_the_incoming_role_survives_because_a_role_has_no_created_at(
    cleanup_rig: CleanupRig,
) -> None:
    rig = cleanup_rig
    _world(rig, incoming=ROLE_ABSORBED, existing=ROLE_SURVIVOR)

    approval = _request(rig, FakeApprovalStore())

    preview = approval.merges[0].preview
    assert (preview.survivor_id, preview.absorbed_id) == (ROLE_ABSORBED, ROLE_SURVIVOR)


def test_acknowledging_a_governance_change_is_refused_for_a_role_with_no_approval(
    cleanup_rig: CleanupRig,
) -> None:
    rig = cleanup_rig
    _world(rig)
    store = FakeApprovalStore()
    before = _graph_state(rig)

    with pytest.raises(NearMissBatchRefusedError, match="acknowledgment"):
        _request(rig, store, acknowledge=True)

    assert approval_row_count(store) == 0
    assert _graph_state(rig) == before


def test_a_role_review_whose_side_was_already_absorbed_is_stale_and_creates_no_approval(
    cleanup_rig: CleanupRig,
) -> None:
    rig = cleanup_rig
    _world(rig)
    del rig.graph.nodes[("Role", ROLE_ABSORBED)]  # a Role merge absorbed it since the listing
    store = FakeApprovalStore()

    with pytest.raises(NearMissBatchRefusedError, match="stale"):
        _request(rig, store)

    assert approval_row_count(store) == 0


# -- signing: the merge, its review and its audit row -------------------------------------------


def test_signing_moves_the_losers_obligations_leaves_a_marker_and_deletes_the_review(
    cleanup_rig: CleanupRig,
) -> None:
    rig = cleanup_rig
    _world(rig)
    store = FakeApprovalStore()
    approval = _request(rig, store)
    row = _sign(store, approval.pending_approval_id)

    outcome = execute_near_miss_batch(row, CONFIG, rig.dependencies(roles()))

    assert [(m["state"], m["winner_id"], m["loser_id"]) for m in _merges(outcome)] == [
        ("merged", ROLE_SURVIVOR, ROLE_ABSORBED)
    ]
    for obligation_id in ("obl_msa_moved_1", "obl_msa_moved_2"):
        assert _has_sources(rig, obligation_id) == [ROLE_SURVIVOR]
    assert ("Role", ROLE_ABSORBED) not in rig.graph.nodes
    assert rig.graph.nodes[("MergedRole", ROLE_ABSORBED)]["merged_into"] == ROLE_SURVIVOR
    assert ("PendingReview", _REVIEW) not in rig.graph.nodes
    assert len(rig.cleanup_groups()) == 1


def test_the_merge_of_a_role_review_can_still_be_unmerged(cleanup_rig: CleanupRig) -> None:
    rig = cleanup_rig
    _world(rig)
    store = FakeApprovalStore()
    approval = _request(rig, store)
    execute_near_miss_batch(
        _sign(store, approval.pending_approval_id), CONFIG, rig.dependencies(roles())
    )
    [applied] = rig.audit.rows
    rig.audit.history = [
        AuditEventRow(
            id="evt-merge",
            occurred_at=datetime(2026, 10, 5, tzinfo=UTC),
            actor_subject=applied.actor_subject,
            actor_issuer=applied.actor_issuer,
            action=applied.action,
            resource_type=applied.resource_type,
            resource_id=applied.resource_id,
            outcome="applied",
            details=copy.deepcopy(applied.details),
        )
    ]
    unmerge_store = FakeApprovalStore()

    unmerge = create_unmerge_approval(
        rig.read,
        rig.audit,
        merged_id=ROLE_ABSORBED,
        actor=OFFICER,
        base_url=_BASE_URL,
        store=unmerge_store,
    )
    outcome = execute_unmerge(
        _sign(unmerge_store, unmerge.pending_approval_id), CONFIG, rig.dependencies(roles())
    )

    assert outcome["unmerged"] is True
    assert ("Role", ROLE_ABSORBED) in rig.graph.nodes
    assert ("MergedRole", ROLE_ABSORBED) not in rig.graph.nodes


def test_an_approver_who_lost_the_compliance_officer_grant_is_refused_and_the_review_stays(
    cleanup_rig: CleanupRig,
) -> None:
    rig = cleanup_rig
    _world(rig)
    store = FakeApprovalStore()
    approval = _request(rig, store)
    row = _sign(store, approval.pending_approval_id)
    revoked = FakeAccessRoleStore(expected_owner=OWNER)
    revoked.bootstrap_first_owner(OWNER)  # the officer's grant is gone
    before = _graph_state(rig)

    outcome = execute_near_miss_batch(row, CONFIG, rig.dependencies(revoked))

    [entry] = _merges(outcome)
    assert entry["state"] == "failed"
    assert "no longer holds" in str(entry["error"])
    assert _graph_state(rig) == before
    assert ("PendingReview", _REVIEW) in rig.graph.nodes
    assert rig.logged_groups == []
    [recorded] = rig.audit.rows
    assert (recorded.action, recorded.outcome) == ("role.merge", "failed")


# -- the status of a Role near-miss merge batch --------------------------------------------------


def test_the_status_of_a_signed_role_near_miss_merge_reports_winner_and_loser(
    cleanup_rig: CleanupRig,
) -> None:
    rig = cleanup_rig
    _world(rig)
    store = FakeApprovalStore()
    approval = _request(rig, store)
    row = _sign(store, approval.pending_approval_id)
    outcome = execute_near_miss_batch(row, CONFIG, rig.dependencies(roles()))
    store.set_outcome(approval.pending_approval_id, outcome)

    status = check_near_miss_merge_batch(
        pending_approval_id=approval.pending_approval_id,
        actor=OFFICER,
        store=store,
        config=CONFIG,
        dependencies=rig.dependencies(roles()),
    )

    assert status is not None
    assert status.status == "signed"
    [entry] = status.merges
    assert (entry.review_id, entry.kind, entry.state) == (_REVIEW, "Role", "merged")
    assert (entry.winner_id, entry.loser_id) == (ROLE_SURVIVOR, ROLE_ABSORBED)


def test_the_status_of_a_pending_role_near_miss_merge_is_visible_to_its_requester(
    cleanup_rig: CleanupRig,
) -> None:
    rig = cleanup_rig
    _world(rig)
    store = FakeApprovalStore()
    approval = _request(rig, store)

    status = check_near_miss_merge_batch(
        pending_approval_id=approval.pending_approval_id,
        actor=OFFICER,
        store=store,
        config=CONFIG,
        dependencies=rig.dependencies(roles()),
    )

    assert status is not None
    assert (status.status, status.merges) == ("pending", ())
