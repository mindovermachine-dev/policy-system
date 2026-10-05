"""Pure planner for merge case 3, both capabilities governed (issue #190, slice 13 a).

AC-BI-011 (different policies: rejected before any approval, naming both policies and the
release-governance step), AC-BI-012 (the same policy needs no acknowledgment), D-A1 (the
absorbed capability's `GOVERNED_BY` edge is deleted, the survivor's stays), and E1/D6 (a pair
governed by two non-draft policies has no completion path; the error says so honestly).
"""

from __future__ import annotations

import pytest

from ps_service.graph_cleanup.errors import GraphCleanupValidationError
from ps_service.graph_cleanup.merge_planner import plan_capability_merge
from ps_service.graph_cleanup.models import (
    CapabilityNodeState,
    GoverningPolicy,
    MergeState,
)

_S = "cap_survivor"
_A = "cap_absorbed"
_SAME = GoverningPolicy(id="pol_1", title="Incident Policy", status="approved")
_SURVIVOR_POLICY = GoverningPolicy(id="pol_s", title="Survivor Policy", status="approved")
_ABSORBED_POLICY = GoverningPolicy(id="pol_a", title="Absorbed Policy", status="approved")


def _node(node_id: str) -> CapabilityNodeState:
    return CapabilityNodeState(
        id=node_id, name=f"name of {node_id}", status="active", properties={}
    )


def _state(survivor: GoverningPolicy, absorbed: GoverningPolicy) -> MergeState:
    sets: dict[str, tuple[str, ...]] = {survivor.id: (_S, "cap_other")}
    sets[absorbed.id] = tuple(sorted({*sets.get(absorbed.id, ()), _A}))
    return MergeState(
        survivor=_node(_S),
        absorbed=_node(_A),
        edges=(),
        survivor_policies=(survivor,),
        absorbed_policies=(absorbed,),
        governed_sets=sets,
    )


def _error_text(survivor: GoverningPolicy, absorbed: GoverningPolicy) -> str:
    with pytest.raises(GraphCleanupValidationError) as excinfo:
        plan_capability_merge(_state(survivor, absorbed))
    return str(excinfo.value)


def test_different_policies_are_rejected_naming_both_policies_and_the_release_step() -> None:
    text = _error_text(_SURVIVOR_POLICY, _ABSORBED_POLICY)

    for fragment in (
        "pol_s",
        "Survivor Policy",
        "pol_a",
        "Absorbed Policy",
        "approved",
        "release-capability-governance",
    ):
        assert fragment in text


def test_a_draft_absorbed_policy_points_at_releasing_the_absorbed_capability() -> None:
    draft = GoverningPolicy(id="pol_a", title="Absorbed Policy", status="draft")

    text = _error_text(_SURVIVOR_POLICY, draft)

    assert "release-capability-governance" in text
    assert "cap_absorbed" in text
    assert "no completion path" not in text


def test_only_the_survivors_policy_draft_suggests_swapping_the_sides() -> None:
    draft = GoverningPolicy(id="pol_s", title="Survivor Policy", status="draft")

    text = _error_text(draft, _ABSORBED_POLICY)

    assert "swap" in text
    assert "cap_survivor" in text
    assert "no completion path" not in text


def test_two_approved_policies_are_unresolvable_and_the_error_says_why() -> None:
    text = _error_text(_SURVIVOR_POLICY, _ABSORBED_POLICY)

    assert "no completion path" in text
    assert "fork carries the whole governed set" in text


def test_a_deprecated_policy_counts_as_not_releasable() -> None:
    deprecated = GoverningPolicy(id="pol_a", title="Absorbed Policy", status="deprecated")

    text = _error_text(_SURVIVOR_POLICY, deprecated)

    assert "no completion path" in text


def test_same_policy_is_planned_as_case_three() -> None:
    plan = plan_capability_merge(_state(_SAME, _SAME))

    assert plan.preview.policy_case == 3


def test_same_policy_needs_no_acknowledgment() -> None:
    governance = plan_capability_merge(_state(_SAME, _SAME)).preview.governance

    assert governance is not None
    assert governance.acknowledgment_required is False
    assert governance.policy == _SAME
    assert governance.governed_side == "both"
    assert governance.obligations_coverage_changed == 0


def test_same_policy_governed_set_loses_only_the_absorbed_capability() -> None:
    governance = plan_capability_merge(_state(_SAME, _SAME)).preview.governance

    assert governance is not None
    assert set(governance.governed_set_before) == {_S, _A, "cap_other"}
    assert set(governance.governed_set_after) == {_S, "cap_other"}


def test_same_policy_expected_counts_pin_both_sides_to_the_one_policy() -> None:
    expected = plan_capability_merge(_state(_SAME, _SAME)).expected

    assert (expected.absorbed_governed, expected.survivor_governed) == (1, 1)
    assert expected.absorbed_policy_id == expected.survivor_policy_id == "pol_1"
    assert expected.absorbed_policy_status == expected.survivor_policy_status == "approved"


def test_same_policy_snapshots_delete_the_absorbed_edge_and_keep_the_survivors() -> None:
    plan = plan_capability_merge(_state(_SAME, _SAME))

    before = {(e.rel_type, e.source_id, e.target_id) for e in plan.before.edges}
    after_edges = [(e.rel_type, e.source_id, e.target_id) for e in plan.after.edges]
    assert ("GOVERNED_BY", _A, "pol_1") in before
    assert ("GOVERNED_BY", _S, "pol_1") in before
    assert ("GOVERNED_BY", _A, "pol_1") not in after_edges
    assert after_edges.count(("GOVERNED_BY", _S, "pol_1")) == 1
    assert [n.id for n in plan.before.nodes if n.label == "Policy"] == ["pol_1"]


def test_the_digest_covers_the_shared_policys_status() -> None:
    approved = plan_capability_merge(_state(_SAME, _SAME)).state_digest
    draft = GoverningPolicy(id="pol_1", title="Incident Policy", status="draft")

    assert plan_capability_merge(_state(draft, draft)).state_digest != approved


def test_the_slice_ten_both_governed_restriction_text_is_gone() -> None:
    text = _error_text(_SURVIVOR_POLICY, _ABSORBED_POLICY)

    assert "not supported yet" not in text
