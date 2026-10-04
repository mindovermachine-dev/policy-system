"""Tests for `ps_service.policy_lifecycle.errors` (issue #134, PLAN.md S9).

One test per error class: constructs it, asserts `str(exc)` contains the
expected actionable phrase, and asserts it contains none of the forbidden
substrings that would leak internal detail (AC-BI-015).
"""

from __future__ import annotations

from ps_service.policy_lifecycle.errors import (
    PolicyCapabilityAlreadyGovernedError,
    PolicyCapabilityNotFoundError,
    PolicyDraftAccessDeniedError,
    PolicyGovernanceConflictError,
    PolicyIncompleteForProposalError,
    PolicyInvalidStatusTransitionError,
    PolicyLifecycleGraphUnavailableError,
    PolicyNotFoundError,
    PolicySelfApprovalBlockedError,
    PolicyTitleAlreadyExistsError,
)

_FORBIDDEN_SUBSTRINGS = ("host", "port", "Traceback")


def _assert_no_forbidden_substrings(message: str) -> None:
    for forbidden in _FORBIDDEN_SUBSTRINGS:
        assert forbidden not in message


def test_policy_not_found_error_names_the_looked_up_id() -> None:
    exc = PolicyNotFoundError("policy-42")

    message = str(exc)

    assert "policy-42" in message
    assert "no Policy exists" in message
    _assert_no_forbidden_substrings(message)


def test_policy_draft_access_denied_error_is_fixed_and_leaks_nothing() -> None:
    exc = PolicyDraftAccessDeniedError()

    message = str(exc)

    assert message == "you do not have access to this Policy"
    _assert_no_forbidden_substrings(message)


def test_policy_title_already_exists_error_names_title_and_existing_id() -> None:
    exc = PolicyTitleAlreadyExistsError("Data Retention Policy", "policy-7")

    message = str(exc)

    assert "Data Retention Policy" in message
    assert "policy-7" in message
    assert "supersede workflow" in message
    _assert_no_forbidden_substrings(message)


def test_policy_incomplete_for_proposal_error_names_standard_requirement() -> None:
    exc = PolicyIncompleteForProposalError()

    message = str(exc)

    assert message == "at least one Standard is required before a Policy can be proposed"
    _assert_no_forbidden_substrings(message)


def test_policy_invalid_status_transition_error_names_action_and_statuses() -> None:
    exc = PolicyInvalidStatusTransitionError(
        action="approve", current_status="draft", required_status="proposed"
    )

    message = str(exc)

    assert "approve" in message
    assert "draft" in message
    assert "proposed" in message
    _assert_no_forbidden_substrings(message)


def test_policy_self_approval_blocked_error_is_fixed_and_leaks_nothing() -> None:
    exc = PolicySelfApprovalBlockedError()

    message = str(exc)

    assert message == "you cannot approve or reject a Policy you own"
    _assert_no_forbidden_substrings(message)


def test_policy_lifecycle_graph_unavailable_error_is_fixed_and_leaks_nothing() -> None:
    exc = PolicyLifecycleGraphUnavailableError()

    message = str(exc)

    assert "policy graph" in message
    _assert_no_forbidden_substrings(message)


def test_error_types_are_distinct_and_not_a_shared_hierarchy() -> None:
    assert not issubclass(PolicyNotFoundError, PolicyDraftAccessDeniedError)
    assert not issubclass(PolicyDraftAccessDeniedError, PolicyTitleAlreadyExistsError)


def test_policy_capability_not_found_error_names_the_supplied_ids() -> None:
    exc = PolicyCapabilityNotFoundError(("cap_a", "cap_b"))

    message = str(exc)

    assert "'cap_a'" in message
    assert "'cap_b'" in message
    assert "no Capability exists" in message
    _assert_no_forbidden_substrings(message)


def test_policy_capability_already_governed_error_names_ids_and_points_at_supersede() -> None:
    exc = PolicyCapabilityAlreadyGovernedError(("cap_a",))

    message = str(exc)

    assert "'cap_a'" in message
    assert "already governed" in message
    assert "supersede" in message
    _assert_no_forbidden_substrings(message)


def test_policy_governance_conflict_error_is_actionable_and_leaks_nothing() -> None:
    exc = PolicyGovernanceConflictError("pol-new")

    message = str(exc)

    assert "pol-new" in message
    assert "nothing was changed" in message
    assert "retry" in message
    _assert_no_forbidden_substrings(message)
