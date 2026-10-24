"""A batch request that cannot be merged as asked is refused whole, naming every offender.

Over a `CleanupRig` these tests create batches that include an empty list, a duplicate, an unknown
review, a stale review, reviews of kinds with no merge, reviews that share a node and Capability
pairs the planner refuses. Each refusal names every offending
review id with its state in one response, creates no approval and leaves the graph unchanged.
"""

from __future__ import annotations

import copy
from typing import TYPE_CHECKING

import pytest
from company_merge._kinds import UNREGISTERED_KIND

from graph_cleanup._fakes import FakeApprovalStore, approval_row_count
from graph_cleanup._merge_runs import OFFICER
from graph_cleanup._near_miss_batch_worlds import (
    seed_capability_pair_with_review,
    seed_policy_governing,
    seed_role_pair_with_review,
)
from ps_service.graph_cleanup.near_miss_batch import create_near_miss_merge_batch
from ps_service.graph_cleanup.near_miss_batch_models import NearMissBatchRefusedError

if TYPE_CHECKING:
    from graph_cleanup._gateway_fakes import CleanupRig
    from ps_service.company_merge.reviewable_kinds import ReviewableKind


def _create(rig: CleanupRig, store: FakeApprovalStore, review_ids: list[str]) -> None:
    create_near_miss_merge_batch(
        rig.read,
        review_ids=review_ids,
        acknowledged_review_ids=[],
        actor=OFFICER,
        base_url="https://ps.example.com",
        store=store,
    )


def _refusal(rig: CleanupRig, review_ids: list[str]) -> NearMissBatchRefusedError:
    """Create the batch, expect its refusal, and check nothing was created or changed."""
    store = FakeApprovalStore()
    before = copy.deepcopy(dict(rig.graph.nodes)), set(rig.graph.edges)
    with pytest.raises(NearMissBatchRefusedError) as raised:
        _create(rig, store, review_ids)
    assert approval_row_count(store) == 0
    assert (copy.deepcopy(dict(rig.graph.nodes)), set(rig.graph.edges)) == before
    return raised.value


def test_an_empty_review_list_is_refused_naming_the_problem(cleanup_rig: CleanupRig) -> None:
    refusal = _refusal(cleanup_rig, [])

    assert "no review ids" in str(refusal)


def test_a_duplicate_review_id_is_refused_naming_the_duplicate(cleanup_rig: CleanupRig) -> None:
    pair = seed_capability_pair_with_review(cleanup_rig, 1)

    refusal = _refusal(cleanup_rig, [pair.review_id, pair.review_id])

    assert pair.review_id in str(refusal)
    assert "more than once" in str(refusal)


def test_an_unknown_review_is_refused_with_its_state(cleanup_rig: CleanupRig) -> None:
    refusal = _refusal(cleanup_rig, ["missing"])

    assert [(f.review_id, f.state) for f in refusal.findings] == [
        ("missing", "no unresolved near-miss review has this id")
    ]


def test_a_review_whose_node_was_absorbed_is_refused_as_stale(cleanup_rig: CleanupRig) -> None:
    pair = seed_capability_pair_with_review(cleanup_rig, 1)
    del cleanup_rig.graph.nodes[("Capability", pair.absorbed_id)]
    cleanup_rig.graph.seed_node(
        ("MergedCapability",), pair.absorbed_id, {"merged_into": pair.survivor_id}
    )

    refusal = _refusal(cleanup_rig, [pair.review_id])

    [finding] = refusal.findings
    assert finding.review_id == pair.review_id
    assert "stale" in finding.state


def test_a_review_of_a_registered_kind_without_a_merge_is_refused_naming_the_kind(
    cleanup_rig: CleanupRig, test_only_reviewable_kind: ReviewableKind
) -> None:
    label = test_only_reviewable_kind.label
    cleanup_rig.seed_endpoint(label, "probe_a", name="Probe A")
    cleanup_rig.seed_endpoint(label, "probe_b", name="Probe B")
    cleanup_rig.seed_near_miss_review(
        "review_probe", incoming_id="probe_a", existing_id="probe_b", kind=label
    )

    refusal = _refusal(cleanup_rig, ["review_probe"])

    [finding] = refusal.findings
    assert finding.review_id == "review_probe"
    assert label in finding.state
    assert "keep it separate" in finding.state


def test_a_review_of_an_unregistered_kind_is_refused_naming_the_kind(
    cleanup_rig: CleanupRig,
) -> None:
    cleanup_rig.seed_near_miss_review(
        "review_stray", incoming_id="stray_a", existing_id="stray_b", kind=UNREGISTERED_KIND
    )

    refusal = _refusal(cleanup_rig, ["review_stray"])

    [finding] = refusal.findings
    assert finding.review_id == "review_stray"
    assert UNREGISTERED_KIND in finding.state


def test_every_offending_review_is_named_in_one_response_and_the_valid_ones_are_not_previewed(
    cleanup_rig: CleanupRig, test_only_reviewable_kind: ReviewableKind
) -> None:
    valid_capability = seed_capability_pair_with_review(cleanup_rig, 1)
    valid_role = seed_role_pair_with_review(cleanup_rig, 1)
    stale = seed_capability_pair_with_review(cleanup_rig, 2)
    del cleanup_rig.graph.nodes[("Capability", stale.absorbed_id)]
    label = test_only_reviewable_kind.label
    cleanup_rig.seed_endpoint(label, "probe_a", name="Probe A")
    cleanup_rig.seed_endpoint(label, "probe_b", name="Probe B")
    cleanup_rig.seed_near_miss_review(
        "review_probe", incoming_id="probe_a", existing_id="probe_b", kind=label
    )

    refusal = _refusal(
        cleanup_rig,
        [
            valid_capability.review_id,
            "missing",
            stale.review_id,
            valid_role.review_id,
            "review_probe",
        ],
    )

    assert [f.review_id for f in refusal.findings] == ["missing", stale.review_id, "review_probe"]
    message = str(refusal)
    for offender in ("missing", stale.review_id, "review_probe"):
        assert offender in message
    assert valid_capability.review_id not in message
    assert valid_role.review_id not in message


def _seed_review_sharing(rig: CleanupRig, n: int, shared_id: str) -> str:
    """Seed a review whose incoming node is `shared_id`, an existing node of another review."""
    other = f"cap_x{n}"
    rig.seed_capability(other, name=f"Other {n}", status="active")
    review = f"review_share_{n}"
    rig.seed_near_miss_review(review, incoming_id=shared_id, existing_id=other)
    return review


def test_two_reviews_sharing_an_entity_are_refused_naming_both_review_ids(
    cleanup_rig: CleanupRig,
) -> None:
    first = seed_capability_pair_with_review(cleanup_rig, 1)
    second_review = _seed_review_sharing(cleanup_rig, 2, first.survivor_id)

    refusal = _refusal(cleanup_rig, [first.review_id, second_review])

    assert [f.review_id for f in refusal.findings] == [first.review_id, second_review]
    for finding in refusal.findings:
        assert first.survivor_id in finding.state
    message = str(refusal)
    assert first.review_id in message
    assert second_review in message


def test_a_chain_of_reviews_sharing_one_node_across_three_reviews_names_every_conflict(
    cleanup_rig: CleanupRig,
) -> None:
    first = seed_capability_pair_with_review(cleanup_rig, 1)
    second = _seed_review_sharing(cleanup_rig, 2, first.survivor_id)
    third = _seed_review_sharing(cleanup_rig, 3, first.survivor_id)
    unrelated = seed_capability_pair_with_review(cleanup_rig, 4)

    refusal = _refusal(cleanup_rig, [first.review_id, second, unrelated.review_id, third])

    assert [f.review_id for f in refusal.findings] == [first.review_id, second, third]
    by_review = {f.review_id: f.state for f in refusal.findings}
    assert second in by_review[first.review_id]
    assert third in by_review[first.review_id]
    assert first.review_id in by_review[second]
    assert third in by_review[second]
    assert unrelated.review_id not in str(refusal)


def test_disjoint_reviews_of_the_same_kind_are_accepted(cleanup_rig: CleanupRig) -> None:
    pairs = [seed_capability_pair_with_review(cleanup_rig, n) for n in (1, 2, 3)]
    store = FakeApprovalStore()

    _create(cleanup_rig, store, [pair.review_id for pair in pairs])

    assert approval_row_count(store) == 1


def test_a_pair_governed_by_two_policies_is_refused_naming_the_release_step_and_the_review(
    cleanup_rig: CleanupRig,
) -> None:
    pair = seed_capability_pair_with_review(cleanup_rig, 1)
    ok = seed_capability_pair_with_review(cleanup_rig, 2)
    seed_policy_governing(cleanup_rig, "pol_a", pair.survivor_id)
    seed_policy_governing(cleanup_rig, "pol_b", pair.absorbed_id)

    refusal = _refusal(cleanup_rig, [pair.review_id, ok.review_id])

    [finding] = refusal.findings
    assert finding.review_id == pair.review_id
    assert "release-capability-governance" in finding.state
    assert pair.review_id in str(refusal)
    assert ok.review_id not in str(refusal)


def test_a_non_active_side_is_refused_naming_the_review(cleanup_rig: CleanupRig) -> None:
    pair = seed_capability_pair_with_review(cleanup_rig, 1)
    cleanup_rig.graph.nodes[("Capability", pair.absorbed_id)]["status"] = "retired"

    refusal = _refusal(cleanup_rig, [pair.review_id])

    [finding] = refusal.findings
    assert finding.review_id == pair.review_id
    assert "not active" in finding.state
