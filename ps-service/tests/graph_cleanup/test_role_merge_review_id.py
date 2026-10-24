"""A `merge-roles` approval may carry the id of the Role `PendingReview` it resolves.

The review must be of kind `Role` and name the survivor and the absorbed Role (in either order);
the id is signed with the pair, the executor re-checks it at sign time and deletes the review as
the last primitive of the merge group, and the `role.merge` audit row records it. Without an id
every behaviour is unchanged.
"""

from __future__ import annotations

import copy
from dataclasses import replace
from datetime import timedelta
from typing import TYPE_CHECKING

import pytest

from graph_cleanup._fakes import (
    ROLE_ABSORBED,
    ROLE_SURVIVOR,
    FakeApprovalStore,
    RecordingAuditStore,
    approval_row_count,
    expire_approval,
)
from graph_cleanup._gateway_fakes import CleanupRig
from graph_cleanup._merge_runs import CONFIG, role_row, run_role_merge
from ps_service.graph_cleanup.audit_actions import RoleMergeDetails
from ps_service.graph_cleanup.dependencies import GraphCleanupDependencies
from ps_service.graph_cleanup.errors import GraphCleanupValidationError
from ps_service.graph_cleanup.graph_reader import read_role_merge_state
from ps_service.graph_cleanup.group_builders import build_role_merge_group
from ps_service.graph_cleanup.service import (
    RECONCILE_GRACE,
    check_cleanup_approval,
    create_role_merge_approval,
)
from ps_service.graph_gateway.models import DeleteNode

if TYPE_CHECKING:
    from collections.abc import Callable

_REVIEW = "review_role_1"
_ACTOR = ("officer", "https://issuer.example.com/")


def _world(
    rig: CleanupRig, *, incoming: str = ROLE_ABSORBED, existing: str = ROLE_SURVIVOR
) -> None:
    rig.seed_role_merge_world()
    rig.seed_near_miss_review(_REVIEW, incoming_id=incoming, existing_id=existing, kind="Role")


def _create(rig: CleanupRig, store: FakeApprovalStore, review_id: str | None = _REVIEW):
    return create_role_merge_approval(
        rig.read,
        survivor_id=ROLE_SURVIVOR,
        absorbed_id=ROLE_ABSORBED,
        review_id=review_id,
        actor=_ACTOR,
        base_url="https://ps.example.com",
        store=store,
    )


def _graph_state(rig: CleanupRig) -> tuple[object, object]:
    return copy.deepcopy(rig.graph.nodes), copy.deepcopy(rig.graph.edges)


# -- preview: the review must name this pair -----------------------------------------------------


def test_the_approval_signs_the_review_id_with_the_pair(cleanup_rig: CleanupRig) -> None:
    _world(cleanup_rig)
    store = FakeApprovalStore()

    approval = _create(cleanup_rig, store)

    row = store.get_by_id(approval.pending_approval_id)
    assert row is not None
    assert row.normalized_args == {
        "survivor_id": ROLE_SURVIVOR,
        "absorbed_id": ROLE_ABSORBED,
        "state_digest": approval.preview.state_digest,
        "review_id": _REVIEW,
    }


def test_a_review_naming_the_pair_the_other_way_round_is_accepted(cleanup_rig: CleanupRig) -> None:
    _world(cleanup_rig, incoming=ROLE_SURVIVOR, existing=ROLE_ABSORBED)
    store = FakeApprovalStore()

    approval = _create(cleanup_rig, store)

    assert approval_row_count(store) == 1
    assert approval.preview.survivor_id == ROLE_SURVIVOR


def test_the_preview_is_the_same_with_and_without_a_review(cleanup_rig: CleanupRig) -> None:
    _world(cleanup_rig)

    with_review = _create(cleanup_rig, FakeApprovalStore())
    without = _create(cleanup_rig, FakeApprovalStore(), review_id=None)

    assert with_review.preview == without.preview


def test_a_review_that_does_not_exist_is_refused_with_no_approval(cleanup_rig: CleanupRig) -> None:
    cleanup_rig.seed_role_merge_world()
    store = FakeApprovalStore()

    with pytest.raises(GraphCleanupValidationError, match="does not exist or was already"):
        _create(cleanup_rig, store)

    assert approval_row_count(store) == 0


def test_a_review_of_another_kind_is_refused_with_no_approval(cleanup_rig: CleanupRig) -> None:
    cleanup_rig.seed_role_merge_world()
    cleanup_rig.seed_near_miss_review(
        _REVIEW, incoming_id=ROLE_ABSORBED, existing_id=ROLE_SURVIVOR, kind="Capability"
    )
    store = FakeApprovalStore()

    with pytest.raises(GraphCleanupValidationError, match="not a Role review"):
        _create(cleanup_rig, store)

    assert approval_row_count(store) == 0


@pytest.mark.parametrize(
    ("incoming", "existing"),
    [(ROLE_ABSORBED, "role_other"), ("role_other", ROLE_SURVIVOR), (ROLE_SURVIVOR, ROLE_SURVIVOR)],
    ids=["other-survivor", "other-absorbed", "same-side-twice"],
)
def test_a_review_naming_another_pair_is_refused_with_no_approval(
    cleanup_rig: CleanupRig, incoming: str, existing: str
) -> None:
    _world(cleanup_rig, incoming=incoming, existing=existing)
    store = FakeApprovalStore()

    with pytest.raises(GraphCleanupValidationError, match="different pair"):
        _create(cleanup_rig, store)

    assert approval_row_count(store) == 0


# -- the write group -----------------------------------------------------------------------------


def test_the_group_deletes_the_pending_review_last(cleanup_rig: CleanupRig) -> None:
    _world(cleanup_rig)
    state = read_role_merge_state(
        cleanup_rig.read, survivor_id=ROLE_SURVIVOR, absorbed_id=ROLE_ABSORBED
    )

    without = build_role_merge_group(state, survivor_id=ROLE_SURVIVOR, absorbed_id=ROLE_ABSORBED)
    with_review = build_role_merge_group(
        state, survivor_id=ROLE_SURVIVOR, absorbed_id=ROLE_ABSORBED, review_id=_REVIEW
    )

    assert with_review[:-1] == without
    assert with_review[-1] == DeleteNode(label="PendingReview", id=_REVIEW)


def test_executing_deletes_the_review_in_the_same_group_as_the_merge(
    cleanup_rig: CleanupRig,
) -> None:
    rig = cleanup_rig
    _world(rig)

    outcome = run_role_merge(rig, row=role_row(rig, review_id=_REVIEW))

    assert outcome == {"survivor_id": ROLE_SURVIVOR, "absorbed_id": ROLE_ABSORBED, "merged": True}
    assert ("PendingReview", _REVIEW) not in rig.graph.nodes
    assert ("Role", ROLE_ABSORBED) not in rig.graph.nodes
    assert rig.graph.nodes[("MergedRole", ROLE_ABSORBED)]["merged_into"] == ROLE_SURVIVOR
    assert len(rig.cleanup_groups()) == 1
    [group] = rig.hooked.groups
    assert group.primitives[-1] == DeleteNode(label="PendingReview", id=_REVIEW)
    assert rig.order() == ["audit:applied", "log_append"]


def test_the_applied_audit_row_names_the_review_and_keeps_both_snapshots(
    cleanup_rig: CleanupRig,
) -> None:
    rig = cleanup_rig
    _world(rig)

    run_role_merge(rig, row=role_row(rig, review_id=_REVIEW))

    [recorded] = rig.audit.rows
    details = RoleMergeDetails.model_validate(recorded.details)
    assert details.review_id == _REVIEW
    assert details.before is not None
    assert details.after is not None


def test_a_merge_without_a_review_id_leaves_a_review_alone_and_records_no_id(
    cleanup_rig: CleanupRig,
) -> None:
    rig = cleanup_rig
    _world(rig)

    run_role_merge(rig)

    assert ("PendingReview", _REVIEW) in rig.graph.nodes
    assert rig.audit.rows[0].details.get("review_id") is None


# -- sign time: the review must still name the pair ----------------------------------------------


def _drop_review(rig: CleanupRig) -> None:
    del rig.graph.nodes[("PendingReview", _REVIEW)]


def _repoint_review(rig: CleanupRig) -> None:
    rig.graph.nodes[("PendingReview", _REVIEW)]["incoming_id"] = "role_other"


def _retype_review(rig: CleanupRig) -> None:
    rig.graph.nodes[("PendingReview", _REVIEW)]["kind"] = "Capability"


@pytest.mark.parametrize(
    "drift",
    [_drop_review, _repoint_review, _retype_review],
    ids=["removed", "repointed", "retyped"],
)
def test_a_review_that_changed_since_the_preview_is_refused_with_a_failed_row(
    cleanup_rig: CleanupRig, drift: Callable[[CleanupRig], None]
) -> None:
    rig = cleanup_rig
    _world(rig)
    row = role_row(rig, review_id=_REVIEW)
    drift(rig)
    before = _graph_state(rig)

    outcome = run_role_merge(rig, row=row)

    assert "changed since the preview" in str(outcome["error"])
    assert _graph_state(rig) == before
    assert rig.logged_groups == []
    [recorded] = rig.audit.rows
    assert recorded.outcome == "failed"
    assert recorded.details["reason_code"] == "review_missing_or_changed"
    assert recorded.details["review_id"] == _REVIEW


@pytest.mark.parametrize("bad", [7, None])
def test_a_non_string_review_id_is_a_malformed_approval(
    cleanup_rig: CleanupRig, bad: object
) -> None:
    rig = cleanup_rig
    _world(rig)
    row = role_row(rig)
    row = replace(row, normalized_args={**row.normalized_args, "review_id": bad})
    before = _graph_state(rig)

    outcome = run_role_merge(rig, row=row)

    assert "error" in outcome
    assert rig.audit.rows == []
    assert _graph_state(rig) == before


# -- status and orphan reconciliation ------------------------------------------------------------


def _check(store: FakeApprovalStore, approval_id: str, audit: RecordingAuditStore):
    return check_cleanup_approval(
        pending_approval_id=approval_id,
        actor=_ACTOR,
        store=store,
        config=CONFIG,
        dependencies=GraphCleanupDependencies(
            open_single_tenant_graph=lambda _c: CleanupRig().read,
            audit_store=lambda _c: audit,
            graph_write_gateway=lambda _c: CleanupRig().gateway,
        ),
    )


def test_the_approval_status_shows_the_review_id(cleanup_rig: CleanupRig) -> None:
    _world(cleanup_rig)
    store = FakeApprovalStore()
    approval = _create(cleanup_rig, store)

    status = _check(store, approval.pending_approval_id, RecordingAuditStore())

    assert status is not None
    assert status.review_id == _REVIEW


def test_an_interrupted_role_merge_failed_row_carries_the_review_id(
    cleanup_rig: CleanupRig,
) -> None:
    _world(cleanup_rig)
    store = FakeApprovalStore()
    approval_id = _create(cleanup_rig, store).pending_approval_id
    assert store.mark_signed(approval_id)
    expire_approval(store, approval_id, expired_for=RECONCILE_GRACE + timedelta(seconds=5))
    audit = RecordingAuditStore()

    _check(store, approval_id, audit)

    [failed] = audit.rows
    assert failed.action == "role.merge"
    assert failed.outcome == "failed"
    assert failed.details["review_id"] == _REVIEW
