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
