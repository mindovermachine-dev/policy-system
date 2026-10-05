"""Pure planner for merge case 2, exactly one capability governed (issue #190, slice 12 a).

AC-BI-010 (preview names the policy, its status and the count of obligations whose coverage
changes; acknowledgment required), CHANGES.md D-A1 (GOVERNED_BY moves with an absorbed
governed capability), M3 (policy status and before/after governed set are recorded), and the
removal of the slice-10 "governed capabilities are not supported yet" restriction for case 2.
"""

from __future__ import annotations

import pytest

from ps_service.graph_cleanup.errors import GraphCleanupValidationError
from ps_service.graph_cleanup.merge_planner import plan_capability_merge
from ps_service.graph_cleanup.models import (
    CapabilityNodeState,
    EdgeRecord,
    GoverningPolicy,
    MergeState,
)

_S = "cap_survivor"
_A = "cap_absorbed"
_APPROVED = GoverningPolicy(id="pol_1", title="Incident Policy", status="approved")
_DRAFT = GoverningPolicy(id="pol_1", title="Incident Policy", status="draft")


def _node(node_id: str) -> CapabilityNodeState:
    return CapabilityNodeState(
        id=node_id, name=f"name of {node_id}", status="active", properties={"description": "d"}
    )


def _requires(obligation: str, target: str) -> EdgeRecord:
    return EdgeRecord(
        rel_type="REQUIRES",
        source_label="Obligation",
        source_id=obligation,
        target_label="Capability",
        target_id=target,
    )


def _state(
    *,
    survivor_policies: tuple[GoverningPolicy, ...] = (),
    absorbed_policies: tuple[GoverningPolicy, ...] = (),
    edges: tuple[EdgeRecord, ...] = (),
    governed_sets: dict[str, tuple[str, ...]] | None = None,
) -> MergeState:
    return MergeState(
        survivor=_node(_S),
        absorbed=_node(_A),
        edges=edges,
        survivor_policies=survivor_policies,
        absorbed_policies=absorbed_policies,
        governed_sets=governed_sets or {},
    )


def _absorbed_governed(policy: GoverningPolicy = _APPROVED) -> MergeState:
    return _state(
        absorbed_policies=(policy,),
        edges=(
            _requires("obl_a1", _A),
            _requires("obl_a2", _A),
            _requires("obl_s1", _S),
            _requires("obl_s2", _S),
            _requires("obl_s2", _A),
        ),
        governed_sets={policy.id: (_A, "cap_other")},
    )


def _survivor_governed(policy: GoverningPolicy = _APPROVED) -> MergeState:
    return _state(
        survivor_policies=(policy,),
        edges=(_requires("obl_a1", _A), _requires("obl_a2", _A), _requires("obl_a2", _S)),
        governed_sets={policy.id: (_S, "cap_other")},
    )


def test_case_two_is_no_longer_rejected_as_unsupported() -> None:
    """The slice-10 restriction is gone for exactly-one-governed, in either direction."""
    for state in (_absorbed_governed(), _survivor_governed()):
        assert plan_capability_merge(state).preview.policy_case == 2


def test_preview_names_the_policy_its_status_and_requires_acknowledgment() -> None:
    governance = plan_capability_merge(_absorbed_governed()).preview.governance

    assert governance is not None
    assert governance.policy == _APPROVED
    assert governance.governed_side == "absorbed"
    assert governance.acknowledgment_required is True


def test_a_case_one_preview_has_no_governance_block() -> None:
    assert plan_capability_merge(_state()).preview.governance is None


def test_coverage_count_when_absorbed_is_governed_is_the_survivors_uncovered_obligations() -> None:
    governance = plan_capability_merge(_absorbed_governed()).preview.governance

    assert governance is not None
    # survivor (ungoverned) is required by obl_s1 and obl_s2; obl_s2 also requires the governed
    # absorbed capability, so only obl_s1 newly gains the policy.
    assert governance.obligations_coverage_changed == 1


def test_coverage_count_when_survivor_is_governed_is_the_absorbed_uncovered_obligations() -> None:
    governance = plan_capability_merge(_survivor_governed()).preview.governance

    assert governance is not None
    assert governance.governed_side == "survivor"
    # absorbed (ungoverned) is required by obl_a1 and obl_a2; obl_a2 also requires the governed
    # survivor, so only obl_a1 newly gains the policy.
    assert governance.obligations_coverage_changed == 1


def test_governed_set_swaps_the_absorbed_for_the_survivor() -> None:
    governance = plan_capability_merge(_absorbed_governed()).preview.governance

    assert governance is not None
    assert governance.governed_set_before == (_A, "cap_other")
    assert governance.governed_set_after == ("cap_other", _S)


def test_governed_set_is_unchanged_when_the_survivor_is_the_governed_side() -> None:
    governance = plan_capability_merge(_survivor_governed()).preview.governance

    assert governance is not None
    assert governance.governed_set_before == ("cap_other", _S)
    assert governance.governed_set_after == ("cap_other", _S)


@pytest.mark.parametrize(("policy", "needle"), [(_APPROVED, "approved"), (_DRAFT, "draft")])
def test_acknowledgment_text_states_the_status_and_what_changes(
    policy: GoverningPolicy, needle: str
) -> None:
    governance = plan_capability_merge(_absorbed_governed(policy)).preview.governance

    assert governance is not None
    text = governance.acknowledgment_text
    assert policy.title in text
    assert f"`{needle}`" in text
    assert "governed set" in text


def test_approved_policy_text_says_content_and_version_are_unchanged() -> None:
    governance = plan_capability_merge(_absorbed_governed(_APPROVED)).preview.governance

    assert governance is not None
    assert (
        "its governed set changes, its content/version does not" in governance.acknowledgment_text
    )


def test_absorbed_governed_moves_the_governed_by_edge_in_the_snapshots() -> None:
    plan = plan_capability_merge(_absorbed_governed())

    before = {(e.rel_type, e.source_id, e.target_id) for e in plan.before.edges}
    after = {(e.rel_type, e.source_id, e.target_id) for e in plan.after.edges}
    assert ("GOVERNED_BY", _A, "pol_1") in before
    assert ("GOVERNED_BY", _A, "pol_1") not in after
    assert ("GOVERNED_BY", _S, "pol_1") in after
    policy_node = next(n for n in plan.before.nodes if n.label == "Policy")
    assert policy_node.id == "pol_1"
    assert policy_node.properties["status"] == "approved"


def test_survivor_governed_keeps_its_governed_by_edge_in_both_snapshots() -> None:
    plan = plan_capability_merge(_survivor_governed())

    for snapshot in (plan.before, plan.after):
        assert ("GOVERNED_BY", _S, "pol_1") in {
            (e.rel_type, e.source_id, e.target_id) for e in snapshot.edges
        }


def test_expected_counts_pin_the_governing_policy_and_its_status() -> None:
    expected = plan_capability_merge(_absorbed_governed()).expected

    assert expected.absorbed_governed == 1
    assert expected.survivor_governed == 0
    assert expected.absorbed_policy_id == "pol_1"
    assert expected.absorbed_policy_status == "approved"
    assert expected.survivor_policy_id is None


def test_the_digest_changes_when_the_policy_status_changes() -> None:
    approved = plan_capability_merge(_absorbed_governed(_APPROVED))
    deprecated = plan_capability_merge(
        _absorbed_governed(
            GoverningPolicy(id="pol_1", title="Incident Policy", status="deprecated")
        )
    )

    assert approved.state_digest != deprecated.state_digest


def test_both_governed_by_different_policies_is_rejected_naming_the_release_step() -> None:
    other = GoverningPolicy(id="pol_2", title="Other", status="approved")
    state = _state(survivor_policies=(other,), absorbed_policies=(_APPROVED,))

    with pytest.raises(GraphCleanupValidationError, match="release-capability-governance"):
        plan_capability_merge(state)


def test_a_capability_with_two_governing_policies_is_rejected_as_a_data_fault() -> None:
    other = GoverningPolicy(id="pol_2", title="Other", status="draft")
    state = _state(absorbed_policies=(_APPROVED, other))

    with pytest.raises(GraphCleanupValidationError, match="more than one governing policy"):
        plan_capability_merge(state)
