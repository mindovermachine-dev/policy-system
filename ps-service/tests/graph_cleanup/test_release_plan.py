"""Pure planner for `release-capability-governance` (issue #190, slice 14 a).

AC-BI-013 (a draft policy's `GOVERNED_BY` edge may be released), AC-BI-014 (an approved policy
is rejected with a pointer to the policy lifecycle), D12 (`deprecated` is rejected like
`approved`), Decision 2 / E1 (a fork carries the whole governed set, so the pointer says so), and
the AC-BI-021 before/after snapshot.
"""

from __future__ import annotations

import pytest

from ps_service.graph_cleanup.errors import GraphCleanupValidationError
from ps_service.graph_cleanup.models import (
    CapabilityNodeState,
    GoverningPolicy,
    ReleaseState,
)
from ps_service.graph_cleanup.release_planner import plan_release_governance

_C = "cap_released"
_DRAFT = GoverningPolicy(id="pol_1", title="Incident Policy", status="draft")


def _node(status: str = "active") -> CapabilityNodeState:
    return CapabilityNodeState(
        id=_C, name="Released capability", status=status, properties={"description": "d"}
    )


def _state(
    *,
    capability: CapabilityNodeState | None = None,
    policies: tuple[GoverningPolicy, ...] = (_DRAFT,),
    governed_set: tuple[str, ...] = (_C, "cap_other"),
) -> ReleaseState:
    return ReleaseState(
        capability=_node() if capability is None else capability,
        policies=policies,
        governed_set=governed_set,
    )


def _error(state: ReleaseState) -> str:
    with pytest.raises(GraphCleanupValidationError) as excinfo:
        plan_release_governance(state)
    return str(excinfo.value)


def test_a_draft_policy_is_releasable_and_the_preview_names_everything() -> None:
    preview = plan_release_governance(_state()).preview

    assert preview.capability_id == _C
    assert preview.capability_name == "Released capability"
    assert (preview.policy_id, preview.policy_title, preview.policy_status) == (
        "pol_1",
        "Incident Policy",
        "draft",
    )
    assert preview.governed_set_before == ("cap_other", _C)
    assert preview.governed_set_after == ("cap_other",)


def test_the_before_snapshot_has_the_edge_and_the_after_snapshot_does_not() -> None:
    plan = plan_release_governance(_state())

    assert [(e.rel_type, e.source_id, e.target_id) for e in plan.before.edges] == [
        ("GOVERNED_BY", _C, "pol_1")
    ]
    assert plan.after.edges == ()
    assert {(n.label, n.id) for n in plan.before.nodes} == {("Capability", _C), ("Policy", "pol_1")}
    assert plan.after.nodes == plan.before.nodes


def test_the_digest_is_stable_and_covers_the_governing_policy() -> None:
    first = plan_release_governance(_state()).state_digest

    assert plan_release_governance(_state()).state_digest == first
    other = GoverningPolicy(id="pol_9", title="Incident Policy", status="draft")
    assert plan_release_governance(_state(policies=(other,))).state_digest != first


@pytest.mark.parametrize("status", ["approved", "deprecated"])
def test_an_approved_or_deprecated_policy_is_rejected_with_a_lifecycle_pointer(
    status: str,
) -> None:
    policy = GoverningPolicy(id="pol_1", title="Incident Policy", status=status)

    text = _error(_state(policies=(policy,)))

    assert "Incident Policy" in text
    assert "pol_1" in text
    assert status in text
    assert "policy lifecycle" in text
    assert "fork carries the whole governed set" in text
    assert "draft" in text


def test_a_proposed_policy_points_at_revert_policy_to_draft() -> None:
    text = _error(_state(policies=(GoverningPolicy(id="pol_1", title="P", status="proposed"),)))

    assert "revert-policy-to-draft" in text
    assert "pol_1" in text


def test_an_ungoverned_capability_is_rejected() -> None:
    assert "not governed" in _error(_state(policies=(), governed_set=()))


def test_a_nonexistent_capability_is_rejected() -> None:
    state = ReleaseState(capability=None, policies=(), governed_set=())

    assert "does not exist" in _error(state)


def test_a_merged_tombstone_is_rejected() -> None:
    assert "merged" in _error(_state(capability=_node("merged")))


def test_a_non_active_capability_is_rejected() -> None:
    assert "not active" in _error(_state(capability=_node("deprecated")))


def test_a_capability_with_two_governing_policies_is_a_data_fault() -> None:
    other = GoverningPolicy(id="pol_2", title="Other", status="draft")

    assert "more than one governing policy" in _error(_state(policies=(_DRAFT, other)))
