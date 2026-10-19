"""A `merge-capabilities` approval may carry the id of the near-miss review it resolves.

The id is signed with the pair; the executor deletes that `PendingReview` as the last primitive
of the merge group and records the id on the `capability.merge` audit row; the approval status
shows it. Without an id every behaviour is unchanged. Tests that only observe edge moves are
characterization of the existing merge write.
"""

from __future__ import annotations

from dataclasses import replace
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
from graph_cleanup._gateway_fakes import CleanupRig
from graph_cleanup._merge_runs import CONFIG, capability_row, run_capability_merge
from ps_service.graph_cleanup.audit_actions import CapabilityMergeDetails
from ps_service.graph_cleanup.dependencies import GraphCleanupDependencies
from ps_service.graph_cleanup.graph_reader import read_capability_merge_state
from ps_service.graph_cleanup.group_builders import build_capability_merge_group
from ps_service.graph_cleanup.service import (
    RECONCILE_GRACE,
    check_cleanup_approval,
    create_capability_merge_approval,
)
from ps_service.graph_gateway.models import DeleteNode
from ps_service.logging import configure

if TYPE_CHECKING:
    from ps_service.passkey_signing.models import PendingApprovalRow

_REVIEW = "review_aaa"
_ACTOR = ("officer", "https://issuer.example.com/")


@pytest.fixture(autouse=True)
def logging_configured() -> None:
    configure()


def _world_with_review(rig: CleanupRig) -> CleanupRig:
    rig.seed_capability_merge_world()
    rig.seed_near_miss_review(_REVIEW, incoming_id=ABSORBED, existing_id=SURVIVOR)
    return rig


def _row_with_review(rig: CleanupRig) -> PendingApprovalRow:
    row = capability_row(rig)
    return replace(row, normalized_args={**row.normalized_args, "review_id": _REVIEW})


def test_the_approval_signs_the_review_id_with_the_pair() -> None:
    store = FakeApprovalStore()

    approval = create_capability_merge_approval(
        ScriptedMergeGraph(),
        survivor_id=SURVIVOR,
        absorbed_id=ABSORBED,
        acknowledge_governance_change=False,
        review_id=_REVIEW,
        actor=_ACTOR,
        base_url="https://ps.example.com",
        store=store,
    )

    row = store.get_by_id(approval.pending_approval_id)
    assert row is not None
    assert row.normalized_args == {
        "survivor_id": SURVIVOR,
        "absorbed_id": ABSORBED,
        "acknowledge_governance_change": False,
        "state_digest": approval.preview.state_digest,
        "review_id": _REVIEW,
    }


def test_the_group_deletes_the_pending_review_last(cleanup_rig: CleanupRig) -> None:
    rig = _world_with_review(cleanup_rig)
    state = read_capability_merge_state(rig.read, survivor_id=SURVIVOR, absorbed_id=ABSORBED)

    without = build_capability_merge_group(state, survivor_id=SURVIVOR, absorbed_id=ABSORBED)
    with_review = build_capability_merge_group(
        state, survivor_id=SURVIVOR, absorbed_id=ABSORBED, review_id=_REVIEW
    )

    assert with_review[:-1] == without
    assert with_review[-1] == DeleteNode(label="PendingReview", id=_REVIEW)


def test_executing_the_approval_replaces_the_loser_with_a_marker_and_deletes_the_review(
    cleanup_rig: CleanupRig,
) -> None:
    rig = _world_with_review(cleanup_rig)

    outcome = run_capability_merge(rig, row=_row_with_review(rig))

    assert outcome == {"survivor_id": SURVIVOR, "absorbed_id": ABSORBED, "merged": True}
    assert ("PendingReview", _REVIEW) not in rig.graph.nodes
    assert ("Capability", ABSORBED) not in rig.graph.nodes
    assert rig.graph.nodes[("MergedCapability", ABSORBED)]["merged_into"] == SURVIVOR
    assert not [key for key in rig.graph.edges if ABSORBED in (key[2], key[4])]
    # Characterization: COVERS moves with the merge (the legacy near-miss write dropped it).
    assert any(key[0] == "COVERS" and key[4] == SURVIVOR for key in rig.graph.edges)


def test_the_applied_audit_row_names_the_review_and_keeps_both_snapshots(
    cleanup_rig: CleanupRig,
) -> None:
    rig = _world_with_review(cleanup_rig)

    run_capability_merge(rig, row=_row_with_review(rig))

    [recorded] = rig.audit.rows
    details = CapabilityMergeDetails.model_validate(recorded.details)
    assert details.review_id == _REVIEW
    assert details.before is not None
    assert details.after is not None


def test_a_merge_without_a_review_id_leaves_other_nodes_alone_and_records_no_id(
    cleanup_rig: CleanupRig,
) -> None:
    rig = _world_with_review(cleanup_rig)

    run_capability_merge(rig)

    assert ("PendingReview", _REVIEW) in rig.graph.nodes
    assert rig.audit.rows[0].details.get("review_id") is None


@pytest.mark.parametrize("bad", [7, None])
def test_a_non_string_review_id_is_a_malformed_approval(
    cleanup_rig: CleanupRig, bad: object
) -> None:
    rig = _world_with_review(cleanup_rig)
    row = capability_row(rig)
    row = replace(row, normalized_args={**row.normalized_args, "review_id": bad})

    outcome = run_capability_merge(rig, row=row)

    assert "error" in outcome
    assert rig.audit.rows == []
    assert ("MergedCapability", ABSORBED) not in rig.graph.nodes
    assert ("Capability", ABSORBED) in rig.graph.nodes


def _create_with_review(store: FakeApprovalStore) -> str:
    approval = create_capability_merge_approval(
        ScriptedMergeGraph(),
        survivor_id=SURVIVOR,
        absorbed_id=ABSORBED,
        acknowledge_governance_change=False,
        review_id=_REVIEW,
        actor=_ACTOR,
        base_url="https://ps.example.com",
        store=store,
    )
    return approval.pending_approval_id


def _check(store: FakeApprovalStore, approval_id: str, audit: RecordingAuditStore):
    return check_cleanup_approval(
        pending_approval_id=approval_id,
        actor=_ACTOR,
        store=store,
        config=CONFIG,
        dependencies=GraphCleanupDependencies(
            open_single_tenant_graph=lambda _c: ScriptedMergeGraph(marker_count=0),
            audit_store=lambda _c: audit,
            graph_write_gateway=lambda _c: CleanupRig().gateway,
        ),
    )


def test_the_approval_status_shows_the_review_id() -> None:
    store = FakeApprovalStore()

    status = _check(store, _create_with_review(store), RecordingAuditStore())

    assert status is not None
    assert status.review_id == _REVIEW


def test_an_interrupted_merge_failed_row_carries_the_review_id() -> None:
    store = FakeApprovalStore()
    approval_id = _create_with_review(store)
    assert store.mark_signed(approval_id)
    expire_approval(store, approval_id, expired_for=RECONCILE_GRACE + timedelta(seconds=5))
    audit = RecordingAuditStore()

    _check(store, approval_id, audit)

    [failed] = audit.rows
    assert failed.outcome == "failed"
    assert failed.details["review_id"] == _REVIEW
