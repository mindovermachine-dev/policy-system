"""Tests for `ps_service.policy_lifecycle.audit_actions` (issue #134, PLAN.md S10).

Proves the `policy.*` actions resolve to the right typed `details` model,
and that Pydantic's existing generic validation (reused from
`ps_service.audit.models.AuditDetails`, no new validation machinery written
here) rejects a `details` payload missing a required field or carrying an
unregistered `reason_code` value -- mirrors
`ps-service/tests/audit/test_models.py`'s own direct-construction style.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from ps_service.audit.models import resolve_details_model
from ps_service.policy_lifecycle.audit_actions import (
    PolicyCreateDraftDetails,
    PolicyTransitionDetails,
)

_TRANSITION_ACTIONS = (
    "policy.propose",
    "policy.approve",
    "policy.reject",
    "policy.revert",
    "policy.auto_deprecate",
)


def test_create_draft_action_resolves_to_its_details_model() -> None:
    assert resolve_details_model("policy.create_draft") is PolicyCreateDraftDetails


@pytest.mark.parametrize("action", _TRANSITION_ACTIONS)
def test_each_transition_action_resolves_to_the_shared_transition_details_model(
    action: str,
) -> None:
    assert resolve_details_model(action) is PolicyTransitionDetails


def test_create_draft_details_accepts_a_well_formed_applied_payload() -> None:
    details = PolicyCreateDraftDetails.model_validate(
        {"affected_node_ids": ("policy-1",), "to_status": "draft"}
    )

    assert details.reason_code is None


def test_create_draft_details_accepts_a_well_formed_rejected_payload() -> None:
    details = PolicyCreateDraftDetails.model_validate(
        {
            "affected_node_ids": (),
            "to_status": "draft",
            "reason_code": "title_already_exists",
        }
    )

    assert details.reason_code == "title_already_exists"


def test_create_draft_details_accepts_a_supersedes_policy_id_payload() -> None:
    """Issue #136, TASK.md's Implementation-decisions paragraph:
    `supersedes_policy_id` was added directly to this existing model rather
    than a new/separate one -- this proves a fork's real payload shape
    validates through the SAME registered model the ordinary create-draft
    path uses, and that `policy.create_draft` still resolves to
    `PolicyCreateDraftDetails` -- a regression guard against someone
    accidentally registering a different model for this action name, or the
    field getting dropped from the model, either of which would make the
    fork's real `AuditStore.record`/`record_standalone` call raise
    `AuditInvalidDetailsError` in production.
    """
    assert resolve_details_model("policy.create_draft") is PolicyCreateDraftDetails

    details = PolicyCreateDraftDetails.model_validate(
        {
            "affected_node_ids": ("policy-2", "std-1"),
            "to_status": "draft",
            "supersedes_policy_id": "policy-1",
        }
    )

    assert details.supersedes_policy_id == "policy-1"


def test_create_draft_details_rejects_an_unregistered_reason_code() -> None:
    with pytest.raises(ValidationError):
        PolicyCreateDraftDetails.model_validate(
            {
                "affected_node_ids": ("policy-1",),
                "to_status": "draft",
                "reason_code": "not_a_real_reason",
            }
        )


def test_transition_details_accepts_a_well_formed_applied_payload() -> None:
    details = PolicyTransitionDetails.model_validate(
        {
            "affected_node_ids": ("policy-1",),
            "from_status": "proposed",
            "to_status": "approved",
        }
    )

    assert details.reason_code is None


def test_transition_details_rejects_a_payload_missing_to_status() -> None:
    with pytest.raises(ValidationError):
        PolicyTransitionDetails.model_validate(
            {"affected_node_ids": ("policy-1",), "from_status": "proposed"}
        )


def test_transition_details_rejects_an_unregistered_reason_code() -> None:
    with pytest.raises(ValidationError):
        PolicyTransitionDetails.model_validate(
            {
                "affected_node_ids": ("policy-1",),
                "from_status": "proposed",
                "to_status": "proposed",
                "reason_code": "not_a_real_reason",
            }
        )


def test_transition_details_accepts_every_registered_reason_code() -> None:
    for reason_code in (
        "access_denied",
        "self_approval_blocked",
        "invalid_status",
        "incomplete_for_proposal",
    ):
        details = PolicyTransitionDetails.model_validate(
            {
                "affected_node_ids": (),
                "from_status": "draft",
                "to_status": "draft",
                "reason_code": reason_code,
            }
        )
        assert details.reason_code == reason_code


def test_create_draft_details_accepts_capability_ids_and_new_reason_codes() -> None:
    """Issue #185: a fresh draft's audit payload names the Capabilities it claims."""
    applied = PolicyCreateDraftDetails.model_validate(
        {"affected_node_ids": ("policy-1",), "capability_ids": ("cap_a", "cap_b")}
    )
    assert applied.capability_ids == ("cap_a", "cap_b")

    for code in ("capability_not_found", "capability_already_governed"):
        rejected = PolicyCreateDraftDetails.model_validate(
            {"affected_node_ids": (), "reason_code": code}
        )
        assert rejected.reason_code == code


def test_create_draft_details_capability_ids_defaults_to_empty() -> None:
    details = PolicyCreateDraftDetails.model_validate({"affected_node_ids": ("policy-1",)})

    assert details.capability_ids == ()


def test_transition_details_accepts_capability_ids_and_governance_conflict() -> None:
    details = PolicyTransitionDetails.model_validate(
        {
            "affected_node_ids": ("policy-2",),
            "from_status": "proposed",
            "to_status": "approved",
            "capability_ids": ("cap_a",),
            "reason_code": "governance_conflict",
        }
    )

    assert details.capability_ids == ("cap_a",)
    assert details.reason_code == "governance_conflict"


def test_transition_details_capability_ids_defaults_to_empty() -> None:
    details = PolicyTransitionDetails.model_validate(
        {"affected_node_ids": ("policy-2",), "from_status": "draft", "to_status": "proposed"}
    )

    assert details.capability_ids == ()
