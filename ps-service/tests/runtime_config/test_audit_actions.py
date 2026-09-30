"""Tests for the runtime-config audit actions' registration and typed details (issue #130)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from ps_service.audit import is_known_resource_type, resolve_details_model
from ps_service.runtime_config import audit_actions


def test_actions_registered_with_audit_registry_and_extra_fields_forbidden() -> None:
    set_model = resolve_details_model("runtime_config.set")
    reset_model = resolve_details_model("runtime_config.reset")

    assert set_model is audit_actions.RuntimeConfigSetDetails
    assert reset_model is audit_actions.RuntimeConfigResetDetails
    assert is_known_resource_type("runtime_config")
    with pytest.raises(ValidationError):
        audit_actions.RuntimeConfigSetDetails.model_validate(
            {"key": "k", "new_value": "v", "token": "x"}
        )


def test_details_reject_non_scalar_old_value() -> None:
    with pytest.raises(ValidationError):
        audit_actions.RuntimeConfigSetDetails.model_validate(
            {"key": "k", "old_value": ["a"], "new_value": "v"}
        )
    with pytest.raises(ValidationError):
        audit_actions.RuntimeConfigResetDetails.model_validate(
            {"key": "k", "old_value": {"nested": "blob"}}
        )


def test_reset_details_omit_old_value_when_the_key_was_absent() -> None:
    details = audit_actions.RuntimeConfigResetDetails.model_validate({"key": "k"})

    assert details.model_dump(mode="json", exclude_none=True) == {"key": "k"}
