"""`near_miss.resolve` audit action registration and details model (issue #195, AC-BI-002/010)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from ps_service.api.near_miss_audit_actions import (
    NEAR_MISS_RESOLVE_ACTION,
    NEAR_MISS_REVIEW_RESOURCE_TYPE,
    NearMissResolveDetails,
)
from ps_service.audit import is_known_resource_type, resolve_details_model


def _valid(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "review_id": "review_aaa",
        "kind": "Capability",
        "incoming_id": "capability_in",
        "existing_id": "capability_ex",
        "decision": "keep_separate",
    }
    return {**base, **overrides}


def test_near_miss_resolve_is_registered_with_a_typed_details_model() -> None:
    assert NEAR_MISS_RESOLVE_ACTION == "near_miss.resolve"
    assert resolve_details_model("near_miss.resolve") is NearMissResolveDetails


def test_near_miss_resolve_details_reject_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        NearMissResolveDetails.model_validate(_valid(error="free text"))


def test_near_miss_resolve_decision_values_are_keep_separate_and_merge() -> None:
    NearMissResolveDetails.model_validate(_valid(decision="keep_separate"))
    NearMissResolveDetails.model_validate(_valid(decision="merge", approval_id="appr-1"))
    with pytest.raises(ValidationError):
        NearMissResolveDetails.model_validate(_valid(decision="keep-separate"))


def test_near_miss_resolve_reason_code_is_an_enumerated_value() -> None:
    for code in (
        "review_not_found",
        "review_stale",
        "graph_write_failed",
        "graph_unavailable",
        "unexpected_error",
    ):
        NearMissResolveDetails.model_validate(_valid(reason_code=code))
    with pytest.raises(ValidationError):
        NearMissResolveDetails.model_validate(_valid(reason_code="Traceback: boom"))


def test_near_miss_review_resource_type_is_registered() -> None:
    assert is_known_resource_type(NEAR_MISS_REVIEW_RESOURCE_TYPE)
    assert NEAR_MISS_REVIEW_RESOURCE_TYPE == "near_miss_review"
