"""A governance-changing Capability merge in a batch needs its own acknowledgment.

Over a `CleanupRig`, these tests create batches whose Capability pairs are governed by none, one
or the same Policy, with and without acknowledgments, and look at what is refused, what is asked
for and what the approval binds.
"""

from __future__ import annotations

import copy
from typing import TYPE_CHECKING, cast

import pytest

from graph_cleanup._fakes import FakeApprovalStore, approval_row_count
from graph_cleanup._merge_runs import OFFICER
from graph_cleanup._near_miss_batch_worlds import (
    SeededPair,
    seed_capability_pair_with_review,
    seed_policy_governing,
    seed_role_pair_with_review,
)
from ps_service.graph_cleanup.near_miss_batch import create_near_miss_merge_batch
from ps_service.graph_cleanup.near_miss_batch_models import (
    NearMissBatchAcknowledgmentRequiredError,
    NearMissBatchApproval,
    NearMissBatchRefusedError,
)

if TYPE_CHECKING:
    from graph_cleanup._gateway_fakes import CleanupRig


def _create(
    rig: CleanupRig, store: FakeApprovalStore, review_ids: list[str], acknowledged: list[str]
) -> NearMissBatchApproval:
    return create_near_miss_merge_batch(
        rig.read,
        review_ids=review_ids,
        acknowledged_review_ids=acknowledged,
        actor=OFFICER,
        base_url="https://ps.example.com",
        store=store,
    )


def _case_two_pair(rig: CleanupRig, n: int) -> SeededPair:
    """A pair whose absorbed Capability alone is governed by an approved Policy."""
    pair = seed_capability_pair_with_review(rig, n)
    seed_policy_governing(rig, f"pol_{n}", pair.absorbed_id)
    return pair


def _snapshot(rig: CleanupRig) -> object:
    return copy.deepcopy(dict(rig.graph.nodes)), set(rig.graph.edges)


def test_a_case_two_review_without_its_acknowledgment_asks_for_it_naming_review_and_policy(
    cleanup_rig: CleanupRig,
) -> None:
    pair = _case_two_pair(cleanup_rig, 1)
    store = FakeApprovalStore()
    before = _snapshot(cleanup_rig)

    with pytest.raises(NearMissBatchAcknowledgmentRequiredError) as raised:
        _create(cleanup_rig, store, [pair.review_id], [])

    [needed] = raised.value.reviews
    assert (needed.review_id, needed.policy_id) == (pair.review_id, "pol_1")
    assert "Policy pol_1" in needed.policy_title
    assert "pol_1" in str(raised.value)
    assert [entry.review_id for entry in raised.value.merges] == [pair.review_id]
    assert approval_row_count(store) == 0
    assert _snapshot(cleanup_rig) == before


def test_with_the_acknowledgment_the_approval_binds_the_flag_per_item(
    cleanup_rig: CleanupRig,
) -> None:
    acknowledged = _case_two_pair(cleanup_rig, 1)
    plain = seed_capability_pair_with_review(cleanup_rig, 2)
    store = FakeApprovalStore()

    approval = _create(
        cleanup_rig, store, [acknowledged.review_id, plain.review_id], [acknowledged.review_id]
    )

    row = store.get_by_id(approval.pending_approval_id)
    assert row is not None
    merges = cast("list[dict[str, object]]", row.normalized_args["merges"])
    assert [(m["review_id"], m["acknowledge_governance_change"]) for m in merges] == [
        (acknowledged.review_id, True),
        (plain.review_id, False),
    ]
    assert [entry.acknowledged for entry in approval.merges] == [True, False]


def test_a_case_three_review_needs_no_acknowledgment(cleanup_rig: CleanupRig) -> None:
    pair = seed_capability_pair_with_review(cleanup_rig, 1)
    seed_policy_governing(cleanup_rig, "pol_1", pair.survivor_id, pair.absorbed_id)
    store = FakeApprovalStore()

    approval = _create(cleanup_rig, store, [pair.review_id], [])

    assert approval_row_count(store) == 1
    assert approval.merges[0].acknowledged is False


def test_an_acknowledgment_for_a_role_review_is_refused_and_no_approval_is_created(
    cleanup_rig: CleanupRig,
) -> None:
    role = seed_role_pair_with_review(cleanup_rig, 1)
    store = FakeApprovalStore()

    with pytest.raises(NearMissBatchRefusedError) as raised:
        _create(cleanup_rig, store, [role.review_id], [role.review_id])

    [finding] = raised.value.findings
    assert finding.review_id == role.review_id
    assert "Role" in finding.state
    assert approval_row_count(store) == 0


def test_an_acknowledgment_for_a_case_one_review_is_refused(cleanup_rig: CleanupRig) -> None:
    pair = seed_capability_pair_with_review(cleanup_rig, 1)
    store = FakeApprovalStore()

    with pytest.raises(NearMissBatchRefusedError) as raised:
        _create(cleanup_rig, store, [pair.review_id], [pair.review_id])

    [finding] = raised.value.findings
    assert finding.review_id == pair.review_id
    assert "no governance change" in finding.state
    assert approval_row_count(store) == 0


def test_an_acknowledgment_for_a_review_that_is_not_selected_is_refused(
    cleanup_rig: CleanupRig,
) -> None:
    selected = seed_capability_pair_with_review(cleanup_rig, 1)
    store = FakeApprovalStore()

    with pytest.raises(NearMissBatchRefusedError) as raised:
        _create(cleanup_rig, store, [selected.review_id], ["review_elsewhere"])

    [finding] = raised.value.findings
    assert finding.review_id == "review_elsewhere"
    assert "not among the selected reviews" in finding.state
    assert approval_row_count(store) == 0


def test_only_the_case_two_reviews_of_a_mixed_batch_are_listed_as_needing_acknowledgment(
    cleanup_rig: CleanupRig,
) -> None:
    first = _case_two_pair(cleanup_rig, 1)
    role = seed_role_pair_with_review(cleanup_rig, 1)
    plain = seed_capability_pair_with_review(cleanup_rig, 2)
    second = _case_two_pair(cleanup_rig, 3)
    store = FakeApprovalStore()

    with pytest.raises(NearMissBatchAcknowledgmentRequiredError) as raised:
        _create(
            cleanup_rig,
            store,
            [first.review_id, role.review_id, plain.review_id, second.review_id],
            [],
        )

    assert [r.review_id for r in raised.value.reviews] == [first.review_id, second.review_id]
    assert [entry.review_id for entry in raised.value.merges] == [
        first.review_id,
        role.review_id,
        plain.review_id,
        second.review_id,
    ]
    assert approval_row_count(store) == 0


def test_one_acknowledged_and_one_missing_still_asks_only_for_the_missing_one(
    cleanup_rig: CleanupRig,
) -> None:
    done = _case_two_pair(cleanup_rig, 1)
    missing = _case_two_pair(cleanup_rig, 2)
    store = FakeApprovalStore()

    with pytest.raises(NearMissBatchAcknowledgmentRequiredError) as raised:
        _create(cleanup_rig, store, [done.review_id, missing.review_id], [done.review_id])

    assert [r.review_id for r in raised.value.reviews] == [missing.review_id]
    assert approval_row_count(store) == 0
