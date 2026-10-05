"""`capability.merge` audit action registration (issue #190, slice 10 sub-step c; AC-BI-021/023)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from graph_cleanup._fakes import ABSORBED, SURVIVOR
from ps_service.audit.models import is_known_resource_type, resolve_details_model
from ps_service.graph_cleanup import audit_actions
from ps_service.graph_cleanup.models import EdgeRecord, GraphSnapshot, NodeRecord

_SNAPSHOT = GraphSnapshot(
    nodes=(
        NodeRecord(label="Capability", id=SURVIVOR, properties={"name": "S", "status": "active"}),
    ),
    edges=(
        EdgeRecord(
            rel_type="REQUIRES",
            source_label="Obligation",
            source_id="obl_1",
            target_label="Capability",
            target_id=SURVIVOR,
        ),
    ),
)


def _details(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "survivor_id": SURVIVOR,
        "absorbed_id": ABSORBED,
        "policy_case": 1,
        "acknowledged": False,
        "approval_id": "approval-1",
        "before": _SNAPSHOT.model_dump(),
        "after": _SNAPSHOT.model_dump(),
    }
    return {**base, **overrides}


def test_the_action_and_its_resource_type_are_registered() -> None:
    assert resolve_details_model("capability.merge") is audit_actions.CapabilityMergeDetails
    assert is_known_resource_type("capability")


def test_details_round_trip_the_before_and_after_snapshots() -> None:
    model = audit_actions.CapabilityMergeDetails.model_validate(_details())

    dumped = model.model_dump(mode="json", exclude_none=True)
    assert dumped["before"] == _SNAPSHOT.model_dump(mode="json")
    assert audit_actions.CapabilityMergeDetails.model_validate(dumped) == model


def test_an_undeclared_field_is_rejected() -> None:
    with pytest.raises(ValidationError):
        audit_actions.CapabilityMergeDetails.model_validate(_details(extra_field="x"))


def test_a_failed_row_may_omit_the_snapshots_but_carries_a_reason_code() -> None:
    model = audit_actions.CapabilityMergeDetails.model_validate(
        _details(before=None, after=None, reason_code="interrupted_no_effect")
    )

    assert model.before is None
    assert model.reason_code == "interrupted_no_effect"


def test_an_unknown_reason_code_is_rejected() -> None:
    with pytest.raises(ValidationError):
        audit_actions.CapabilityMergeDetails.model_validate(_details(reason_code="whatever"))


_OBLIGATION_SNAPSHOT = GraphSnapshot(
    nodes=(NodeRecord(label="MergedObligation", id="obl_a", properties={"merged_into": "obl_s"}),),
    edges=(
        EdgeRecord(
            rel_type="HAS",
            source_label="Role",
            source_id="role_1",
            target_label="Obligation",
            target_id="obl_s",
        ),
    ),
)


def _obligation_details(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "survivor_id": "obl_s",
        "absorbed_id": "obl_a",
        "role_id": "role_1",
        "approval_id": "approval-1",
        "before": _OBLIGATION_SNAPSHOT.model_dump(),
        "after": _OBLIGATION_SNAPSHOT.model_dump(),
    }
    return {**base, **overrides}


def test_the_obligation_merge_action_and_its_resource_type_are_registered() -> None:
    assert resolve_details_model("obligation.merge") is audit_actions.ObligationMergeDetails
    assert is_known_resource_type("obligation")


def test_obligation_details_round_trip_the_snapshots() -> None:
    model = audit_actions.ObligationMergeDetails.model_validate(_obligation_details())

    dumped = model.model_dump(mode="json", exclude_none=True)
    assert dumped["before"] == _OBLIGATION_SNAPSHOT.model_dump(mode="json")
    assert audit_actions.ObligationMergeDetails.model_validate(dumped) == model


def test_obligation_details_reject_an_undeclared_field() -> None:
    with pytest.raises(ValidationError):
        audit_actions.ObligationMergeDetails.model_validate(_obligation_details(extra_field="x"))


def test_a_failed_obligation_row_may_omit_the_snapshots_but_carries_a_reason_code() -> None:
    model = audit_actions.ObligationMergeDetails.model_validate(
        {
            "survivor_id": "obl_s",
            "absorbed_id": "obl_a",
            "role_id": "role_1",
            "approval_id": "approval-1",
            "reason_code": "interrupted_no_effect",
        }
    )

    assert model.before is None
    assert model.reason_code == "interrupted_no_effect"


def test_case_two_details_carry_the_policy_status_and_the_governed_sets() -> None:
    model = audit_actions.CapabilityMergeDetails.model_validate(
        _details(
            policy_case=2,
            acknowledged=True,
            policy_id="pol_1",
            policy_status="approved",
            governed_set_before=[ABSORBED, "cap_other"],
            governed_set_after=["cap_other", SURVIVOR],
        )
    )

    dumped = model.model_dump(mode="json", exclude_none=True)
    assert dumped["policy_id"] == "pol_1"
    assert dumped["policy_status"] == "approved"
    assert dumped["governed_set_before"] == [ABSORBED, "cap_other"]
    assert dumped["governed_set_after"] == ["cap_other", SURVIVOR]


def test_case_one_details_omit_the_policy_fields() -> None:
    dumped = audit_actions.CapabilityMergeDetails.model_validate(_details()).model_dump(
        mode="json", exclude_none=True
    )

    assert "policy_id" not in dumped
    assert "governed_set_before" not in dumped


def _release_details(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "capability_id": SURVIVOR,
        "policy_id": "pol_1",
        "policy_status": "draft",
        "approval_id": "approval-1",
        "before": _SNAPSHOT.model_dump(),
        "after": _SNAPSHOT.model_dump(),
        "governed_set_before": [SURVIVOR, "cap_other"],
        "governed_set_after": ["cap_other"],
    }
    return {**base, **overrides}


def test_the_release_governance_action_is_registered_on_the_capability_resource_type() -> None:
    assert (
        resolve_details_model("capability.release_governance")
        is audit_actions.CapabilityReleaseGovernanceDetails
    )
    assert audit_actions.CAPABILITY_RELEASE_GOVERNANCE_ACTION == "capability.release_governance"


def test_release_details_round_trip_the_snapshots_and_the_governed_sets() -> None:
    model = audit_actions.CapabilityReleaseGovernanceDetails.model_validate(_release_details())

    dumped = model.model_dump(mode="json", exclude_none=True)
    assert dumped["before"] == _SNAPSHOT.model_dump(mode="json")
    assert dumped["governed_set_after"] == ["cap_other"]
    assert audit_actions.CapabilityReleaseGovernanceDetails.model_validate(dumped) == model


def test_a_failed_release_row_may_omit_the_snapshots_but_carries_a_reason_code() -> None:
    model = audit_actions.CapabilityReleaseGovernanceDetails.model_validate(
        _release_details(
            before=None,
            after=None,
            governed_set_before=None,
            governed_set_after=None,
            reason_code="graph_guard_missed",
        )
    )

    assert model.before is None
    assert model.reason_code == "graph_guard_missed"


def test_an_undeclared_release_field_is_rejected() -> None:
    with pytest.raises(ValidationError):
        audit_actions.CapabilityReleaseGovernanceDetails.model_validate(
            _release_details(extra_field="x")
        )


def _unmerge_details(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "survivor_id": SURVIVOR,
        "absorbed_id": ABSORBED,
        "policy_case": 1,
        "approval_id": "approval-2",
        "reverses_approval_id": "approval-1",
        "before": _SNAPSHOT.model_dump(),
        "after": _SNAPSHOT.model_dump(),
        "restored_edges": [_SNAPSHOT.edges[0].model_dump()],
        "survivor_added_edges": [],
    }
    return {**base, **overrides}


def test_the_capability_unmerge_action_is_registered_on_the_capability_resource_type() -> None:
    assert resolve_details_model("capability.unmerge") is audit_actions.CapabilityUnmergeDetails
    assert audit_actions.CAPABILITY_UNMERGE_ACTION == "capability.unmerge"


def test_unmerge_details_round_trip_snapshots_restored_and_survivor_added_edges() -> None:
    model = audit_actions.CapabilityUnmergeDetails.model_validate(
        _unmerge_details(survivor_added_edges=[_SNAPSHOT.edges[0].model_dump()])
    )

    dumped = model.model_dump(mode="json", exclude_none=True)
    assert dumped["reverses_approval_id"] == "approval-1"
    assert dumped["before"] == _SNAPSHOT.model_dump(mode="json")
    assert dumped["restored_edges"] == [_SNAPSHOT.edges[0].model_dump(mode="json")]
    assert dumped["survivor_added_edges"] == [_SNAPSHOT.edges[0].model_dump(mode="json")]
    assert audit_actions.CapabilityUnmergeDetails.model_validate(dumped) == model


def test_a_failed_unmerge_row_may_omit_the_snapshots_but_carries_a_reason_code() -> None:
    model = audit_actions.CapabilityUnmergeDetails.model_validate(
        {
            "survivor_id": SURVIVOR,
            "absorbed_id": ABSORBED,
            "approval_id": "approval-2",
            "reverses_approval_id": "approval-1",
            "reason_code": "interrupted_no_effect",
        }
    )

    assert model.before is None
    assert model.reason_code == "interrupted_no_effect"


def test_unmerge_details_reject_an_undeclared_field() -> None:
    with pytest.raises(ValidationError):
        audit_actions.CapabilityUnmergeDetails.model_validate(_unmerge_details(extra_field="x"))


def _obligation_unmerge_details(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "survivor_id": "obl_s",
        "absorbed_id": "obl_a",
        "role_id": "role_1",
        "approval_id": "approval-2",
        "reverses_approval_id": "approval-1",
        "before": _OBLIGATION_SNAPSHOT.model_dump(),
        "after": _OBLIGATION_SNAPSHOT.model_dump(),
        "restored_edges": [_OBLIGATION_SNAPSHOT.edges[0].model_dump()],
        "survivor_added_edges": [],
        "survivor_edges_possibly_from_merge": [],
    }
    return {**base, **overrides}


def test_the_obligation_unmerge_action_is_registered_on_the_obligation_resource_type() -> None:
    assert resolve_details_model("obligation.unmerge") is audit_actions.ObligationUnmergeDetails
    assert audit_actions.OBLIGATION_UNMERGE_ACTION == "obligation.unmerge"


def test_obligation_unmerge_details_round_trip_snapshots_and_survivor_edge_lists() -> None:
    model = audit_actions.ObligationUnmergeDetails.model_validate(
        _obligation_unmerge_details(
            survivor_edges_possibly_from_merge=[_OBLIGATION_SNAPSHOT.edges[0].model_dump()]
        )
    )

    dumped = model.model_dump(mode="json", exclude_none=True)
    assert dumped["reverses_approval_id"] == "approval-1"
    assert dumped["role_id"] == "role_1"
    assert dumped["survivor_edges_possibly_from_merge"] == [
        _OBLIGATION_SNAPSHOT.edges[0].model_dump(mode="json")
    ]
    assert audit_actions.ObligationUnmergeDetails.model_validate(dumped) == model


def test_a_failed_obligation_unmerge_row_may_omit_the_snapshots() -> None:
    model = audit_actions.ObligationUnmergeDetails.model_validate(
        {
            "survivor_id": "obl_s",
            "absorbed_id": "obl_a",
            "role_id": "role_1",
            "approval_id": "approval-2",
            "reverses_approval_id": "approval-1",
            "reason_code": "interrupted_no_effect",
        }
    )

    assert model.before is None
    assert model.reason_code == "interrupted_no_effect"


def test_obligation_unmerge_details_reject_an_undeclared_field() -> None:
    with pytest.raises(ValidationError):
        audit_actions.ObligationUnmergeDetails.model_validate(
            _obligation_unmerge_details(extra_field="x")
        )
