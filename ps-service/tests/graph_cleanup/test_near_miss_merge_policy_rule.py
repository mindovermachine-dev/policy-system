"""A near-miss merge request obeys the cleanup merge rules: Policy cases, survivor and preview.

`run_request_near_miss_merge` turns a `PendingReview` into a `merge-capabilities` approval. The
tests drive it over a `CleanupRig` (the real reader, planner and executor over an in-memory
graph and gateway), sign the approval the way the router does, and look at the graph afterwards.
Tests whose names say "characterization" observe behaviour the cleanup merge already had.
"""

from __future__ import annotations

import copy
from dataclasses import replace
from typing import TYPE_CHECKING

import pytest
from company_merge._kinds import UNREGISTERED_KIND

from graph_cleanup._fakes import ABSORBED, SURVIVOR, FakeApprovalStore, approval_row_count
from graph_cleanup._merge_runs import CONFIG, OFFICER, roles
from ps_service.api.errors import PendingReviewNotFoundError, ReviewKindUnsupportedError
from ps_service.api.near_miss_review_orchestration import (
    build_default_near_miss_review_dependencies,
    run_request_near_miss_merge,
)
from ps_service.graph_cleanup.errors import (
    GraphCleanupAcknowledgmentRequiredError,
    GraphCleanupValidationError,
)
from ps_service.graph_cleanup.executors import execute_capability_merge
from ps_service.graph_cleanup.service import CapabilityMergeApproval

if TYPE_CHECKING:
    from graph_cleanup._gateway_fakes import CleanupReadGraph, CleanupRig
    from ps_service.company_merge.reviewable_kinds import ReviewableKind

_REVIEW = "review_aaa"
_OLD = "2020-01-01T00:00:00+00:00"
_NEW = "2024-01-01T00:00:00+00:00"
_BASE_URL = "https://ps.example.com"


def _request(
    rig: CleanupRig,
    store: FakeApprovalStore,
    *,
    acknowledge: bool = False,
) -> CapabilityMergeApproval:
    def _open(_config: object) -> CleanupReadGraph:
        return rig.read

    dependencies = replace(
        build_default_near_miss_review_dependencies(), open_single_tenant_graph=_open
    )
    approval = run_request_near_miss_merge(
        _REVIEW,
        actor=OFFICER,
        acknowledge_governance_change=acknowledge,
        base_url=_BASE_URL,
        config=CONFIG,
        dependencies=dependencies,
        store=store,
    )
    assert isinstance(approval, CapabilityMergeApproval)  # a Capability review
    return approval


def _world(
    rig: CleanupRig,
    *,
    governed: tuple[tuple[str, str, str], ...] = (),
    incoming_created_at: str | None = _NEW,
    existing_created_at: str | None = _OLD,
) -> None:
    """ABSORBED is the incoming node, SURVIVOR the existing one (the older: the default winner)."""
    rig.seed_capability_merge_world(governed=governed)
    rig.seed_near_miss_review(
        _REVIEW,
        incoming_id=ABSORBED,
        existing_id=SURVIVOR,
        incoming_created_at=incoming_created_at,
        existing_created_at=existing_created_at,
    )


def _snapshot(rig: CleanupRig) -> object:
    return copy.deepcopy(dict(rig.graph.nodes)), set(rig.graph.edges)


def _governing(rig: CleanupRig, capability_id: str) -> list[str]:
    return [
        key[4] for key in rig.graph.edges if key[0] == "GOVERNED_BY" and key[2] == capability_id
    ]


def _sign_and_execute(
    rig: CleanupRig, store: FakeApprovalStore, approval: CapabilityMergeApproval
) -> dict[str, object]:
    row = store.get_by_id(approval.pending_approval_id)
    assert row is not None
    assert store.mark_signed(row.id)
    signed = store.get_by_id(row.id)
    assert signed is not None
    return execute_capability_merge(signed, CONFIG, rig.dependencies(roles()))


def test_merging_two_governed_capabilities_leaves_the_winner_with_one_governing_policy(
    cleanup_rig: CleanupRig,
) -> None:
    """Two Capabilities governed by different Policies are refused before any approval exists.

    The legacy near-miss write copied the loser's `GOVERNED_BY` onto the winner (two governing
    Policies). The cleanup merge rejects the pair and points at `release-capability-governance`.
    """
    rig = cleanup_rig
    _world(
        rig,
        governed=((SURVIVOR, "pol_winner", "draft"), (ABSORBED, "pol_loser", "draft")),
    )
    store = FakeApprovalStore()
    before = _snapshot(rig)

    with pytest.raises(GraphCleanupValidationError, match="release-capability-governance"):
        _request(rig, store)

    assert approval_row_count(store) == 0
    assert _snapshot(rig) == before
    assert ("PendingReview", _REVIEW) in rig.graph.nodes
    assert _governing(rig, SURVIVOR) == ["pol_winner"]


def test_exactly_one_governed_capability_needs_the_acknowledgment_and_no_approval_without_it(
    cleanup_rig: CleanupRig,
) -> None:
    rig = cleanup_rig
    _world(rig, governed=((ABSORBED, "pol_1", "approved"),))
    store = FakeApprovalStore()
    before = _snapshot(rig)

    with pytest.raises(GraphCleanupAcknowledgmentRequiredError) as raised:
        _request(rig, store)

    assert raised.value.preview.policy_case == 2
    assert approval_row_count(store) == 0
    assert _snapshot(rig) == before


def test_with_the_acknowledgment_the_winner_is_governed_by_the_one_policy_afterwards(
    cleanup_rig: CleanupRig,
) -> None:
    rig = cleanup_rig
    _world(rig, governed=((ABSORBED, "pol_1", "approved"),))
    store = FakeApprovalStore()

    approval = _request(rig, store, acknowledge=True)
    outcome = _sign_and_execute(rig, store, approval)

    assert approval.preview.policy_case == 2
    assert outcome == {"survivor_id": SURVIVOR, "absorbed_id": ABSORBED, "merged": True}
    assert _governing(rig, SURVIVOR) == ["pol_1"]
    assert ("Capability", ABSORBED) not in rig.graph.nodes
    assert rig.graph.nodes[("MergedCapability", ABSORBED)]["merged_into"] == SURVIVOR
    assert ("PendingReview", _REVIEW) not in rig.graph.nodes


def test_characterization_both_governed_by_the_same_policy_needs_no_acknowledgment(
    cleanup_rig: CleanupRig,
) -> None:
    rig = cleanup_rig
    _world(rig, governed=((ABSORBED, "pol_1", "approved"), (SURVIVOR, "pol_1", "approved")))
    store = FakeApprovalStore()

    approval = _request(rig, store)
    _sign_and_execute(rig, store, approval)

    assert approval.preview.policy_case == 3
    assert _governing(rig, SURVIVOR) == ["pol_1"]
    assert _governing(rig, ABSORBED) == []


@pytest.mark.parametrize(
    ("incoming_created", "existing_created", "survivor", "absorbed"),
    [
        pytest.param(_OLD, _NEW, ABSORBED, SURVIVOR, id="incoming-older"),
        pytest.param(_NEW, _OLD, SURVIVOR, ABSORBED, id="existing-older"),
        pytest.param(_OLD, _OLD, ABSORBED, SURVIVOR, id="tie-goes-to-incoming"),
        pytest.param(None, _OLD, ABSORBED, SURVIVOR, id="incoming-missing"),
        pytest.param(_OLD, None, SURVIVOR, ABSORBED, id="existing-missing"),
        pytest.param(None, None, ABSORBED, SURVIVOR, id="both-missing-goes-to-incoming"),
    ],
)
def test_the_survivor_is_the_earlier_created_node_with_ties_going_to_the_incoming_node(
    cleanup_rig: CleanupRig,
    incoming_created: str | None,
    existing_created: str | None,
    survivor: str,
    absorbed: str,
) -> None:
    rig = cleanup_rig
    _world(rig, incoming_created_at=incoming_created, existing_created_at=existing_created)
    store = FakeApprovalStore()

    approval = _request(rig, store)

    assert (approval.preview.survivor_id, approval.preview.absorbed_id) == (survivor, absorbed)
    row = store.get_by_id(approval.pending_approval_id)
    assert row is not None
    assert row.normalized_args["survivor_id"] == survivor
    assert row.normalized_args["absorbed_id"] == absorbed
    assert row.normalized_args["review_id"] == _REVIEW


def test_the_preview_names_the_pair_the_edges_the_policy_case_and_the_digest(
    cleanup_rig: CleanupRig,
) -> None:
    rig = cleanup_rig
    _world(rig)
    store = FakeApprovalStore()
    before = _snapshot(rig)

    approval = _request(rig, store)

    preview = approval.preview.model_dump(mode="json")
    assert preview["survivor_id"] == SURVIVOR
    assert preview["absorbed_id"] == ABSORBED
    assert preview["policy_case"] == 1
    assert preview["edges_to_move"] == {
        "requires": ["obl_1", "obl_2"],
        "covers": ["pa_1"],
        "mitigated_by": [],
    }
    assert isinstance(preview["state_digest"], str)
    assert approval.approval_url.startswith(
        f"{_BASE_URL}/approvals/{approval.pending_approval_id}#"
    )
    assert approval.expires_at
    assert _snapshot(rig) == before


def test_a_missing_review_is_not_found_and_creates_no_approval(cleanup_rig: CleanupRig) -> None:
    rig = cleanup_rig
    rig.seed_capability_merge_world()
    store = FakeApprovalStore()

    with pytest.raises(PendingReviewNotFoundError, match="no unresolved PendingReview"):
        _request(rig, store)

    assert approval_row_count(store) == 0


def test_a_review_whose_node_was_absorbed_is_stale_and_creates_no_approval(
    cleanup_rig: CleanupRig,
) -> None:
    rig = cleanup_rig
    _world(rig)
    del rig.graph.nodes[("Capability", ABSORBED)]
    rig.graph.seed_node(("MergedCapability",), ABSORBED, {"merged_into": SURVIVOR})
    store = FakeApprovalStore()

    with pytest.raises(PendingReviewNotFoundError, match="stale"):
        _request(rig, store)

    assert approval_row_count(store) == 0


def test_a_review_of_a_kind_without_a_merge_is_refused_naming_the_kind(
    cleanup_rig: CleanupRig, test_only_reviewable_kind: ReviewableKind
) -> None:
    rig = cleanup_rig
    rig.seed_capability_merge_world()
    label = test_only_reviewable_kind.label
    rig.seed_endpoint(label, "probe_a", name="Probe A")
    rig.seed_endpoint(label, "probe_b", name="Probe B")
    rig.seed_near_miss_review(_REVIEW, incoming_id="probe_a", existing_id="probe_b", kind=label)
    store = FakeApprovalStore()
    before = copy.deepcopy(rig.graph.nodes)

    with pytest.raises(GraphCleanupValidationError, match=label):
        _request(rig, store)

    assert approval_row_count(store) == 0
    assert rig.graph.nodes == before


def test_a_review_of_an_unregistered_kind_is_refused_naming_the_kind_and_creates_no_approval(
    cleanup_rig: CleanupRig,
) -> None:
    rig = cleanup_rig
    rig.seed_capability_merge_world()
    rig.seed_near_miss_review(
        _REVIEW, incoming_id="stray_a", existing_id="stray_b", kind=UNREGISTERED_KIND
    )
    store = FakeApprovalStore()

    with pytest.raises(ReviewKindUnsupportedError, match=UNREGISTERED_KIND):
        _request(rig, store)

    assert approval_row_count(store) == 0


def test_a_near_miss_merge_moves_requires_covers_and_mitigated_by_and_leaves_only_a_marker(
    cleanup_rig: CleanupRig,
) -> None:
    """Every inbound edge class of the loser ends on the winner; a marker replaces the loser."""
    rig = cleanup_rig
    _world(rig)
    rig.seed_endpoint("RiskPath", "rp_1")
    rig.seed_edge(("MITIGATED_BY", "RiskPath", "rp_1", "Capability", ABSORBED), legacy=False)
    store = FakeApprovalStore()

    _sign_and_execute(rig, store, _request(rig, store))

    def _edges_of(capability_id: str) -> set[tuple[str, str]]:
        return {
            (key[0], key[2])
            for key in rig.graph.edges
            if key[0] in {"REQUIRES", "COVERS", "MITIGATED_BY"} and key[4] == capability_id
        }

    assert _edges_of(SURVIVOR) == {
        ("REQUIRES", "obl_1"),
        ("REQUIRES", "obl_2"),
        ("COVERS", "pa_1"),
        ("MITIGATED_BY", "rp_1"),
    }
    assert _edges_of(ABSORBED) == set()
    assert [key[0] for key in rig.graph.edges if ABSORBED in (key[2], key[4])] == []
    assert ("Capability", ABSORBED) not in rig.graph.nodes
    assert rig.graph.nodes[("MergedCapability", ABSORBED)]["merged_into"] == SURVIVOR
