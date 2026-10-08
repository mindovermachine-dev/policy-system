"""Tests for `ps_service.audit.models` (issue #147, PLAN.md §3.1, Slice 1).

Fast, hermetic -- no Postgres. Proves the extensible action registry
(`register_audit_action`/`resolve_details_model`) and the `AuditDetails`
base type's `extra="forbid"` contract (AC-BI-009) in isolation, before any
store/Postgres code exists.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from ps_service.audit.models import (
    AUDIT_DETAILS_FILTER_KEYS,
    AuditDetails,
    AuditQueryFilters,
    is_known_resource_type,
    register_audit_action,
    register_audit_resource_type,
    resolve_details_model,
)


class _ExampleDetails(AuditDetails):
    """A minimal `AuditDetails` subclass used only by this test module."""

    widget_id: str


def test_resolve_details_model_returns_none_for_an_unregistered_action() -> None:
    """An action nothing has registered resolves to `None`, not a `KeyError`."""
    assert resolve_details_model("nonexistent.action.never_registered") is None


def test_register_audit_action_makes_the_model_resolvable() -> None:
    """A registered action's model is returned verbatim by `resolve_details_model`."""
    action = "test_models.registered_once"

    register_audit_action(action, _ExampleDetails)

    assert resolve_details_model(action) is _ExampleDetails


def test_register_audit_action_raises_on_a_duplicate_action_name() -> None:
    """Registering the same action twice raises, defending against a silent collision."""
    action = "test_models.registered_twice"
    register_audit_action(action, _ExampleDetails)

    with pytest.raises(ValueError, match=action):
        register_audit_action(action, _ExampleDetails)


def test_audit_details_subclass_accepts_its_declared_fields() -> None:
    """A shape matching the model's declared fields validates cleanly."""
    details = _ExampleDetails.model_validate({"widget_id": "abc-123"})

    assert details.widget_id == "abc-123"


def test_audit_details_subclass_rejects_an_undeclared_extra_field() -> None:
    """An extra, undeclared field is rejected outright (AC-BI-009), not silently dropped."""
    with pytest.raises(ValidationError):
        _ExampleDetails.model_validate({"widget_id": "abc-123", "token": "should-not-be-allowed"})


def test_audit_details_subclass_rejects_a_missing_required_field() -> None:
    """A shape missing a required declared field is rejected (AC-BI-005)."""
    with pytest.raises(ValidationError):
        _ExampleDetails.model_validate({})


def test_is_known_resource_type_returns_false_for_an_unregistered_resource_type() -> None:
    """A resource type nothing has registered is not known -- no `KeyError`, just `False`."""
    assert is_known_resource_type("nonexistent.resource_type.never_registered") is False


def test_register_audit_resource_type_makes_it_known() -> None:
    """A registered resource type reads back as known via `is_known_resource_type` (AC-BI-008)."""
    resource_type = "test_models.registered_resource_type"

    register_audit_resource_type(resource_type)

    assert is_known_resource_type(resource_type) is True


def test_register_audit_resource_type_tolerates_being_registered_twice() -> None:
    """Unlike `register_audit_action`, re-registering the same resource type never raises.

    Multiple actions from the same component legitimately share one resource
    type (e.g. this issue's own four `access_role.*` actions all use
    `"principal"`) -- there is nothing to defend against here.
    """
    resource_type = "test_models.registered_resource_type_twice"
    register_audit_resource_type(resource_type)

    register_audit_resource_type(resource_type)  # must not raise

    assert is_known_resource_type(resource_type) is True


def test_audit_details_filter_keys_allow_list_is_exactly_the_three_business_keys() -> None:
    """AC-BI-018: the `details` filter's allow-list is a closed, ordered tuple of three keys."""
    assert AUDIT_DETAILS_FILTER_KEYS == ("celex", "regulatory_instrument_id", "instrument_id")


def test_audit_query_filters_details_defaults_to_empty() -> None:
    """No `details` filter by default, so existing callers are unaffected."""
    assert AuditQueryFilters().details is None
