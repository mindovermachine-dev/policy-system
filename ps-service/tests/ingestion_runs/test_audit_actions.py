"""Tests for the ingestion-run audit actions' registration, typed details and builders (issue #194).

AC-BI-016 / AC-BI-017. `audit_events.outcome` is constrained to `applied|rejected|failed`, so
"outcome=started" is stored as `outcome="applied"` plus `details.status="started"` (OQ-2).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from ps_service.audit import AuditDetails, is_known_resource_type, resolve_details_model
from ps_service.ingestion_runs import audit_actions

_RESULT: dict[str, object] = {
    "run_id": "r",
    "regulatory_instrument_id": "cra-1.0",
    "source": "catalog",
    "outcome": "fresh",
    "stages": [{"stage": "ingestion", "status": "succeeded", "summary": 3}],
}


def test_importing_the_package_registers_both_actions_and_the_resource_type() -> None:
    assert resolve_details_model("ingestion_run.submit") is audit_actions.IngestionRunSubmitDetails
    assert (
        resolve_details_model("ingestion_run.complete") is audit_actions.IngestionRunCompleteDetails
    )
    assert is_known_resource_type("ingestion_run")


def test_the_action_and_resource_type_names_are_the_documented_strings() -> None:
    assert audit_actions.INGESTION_RUN_SUBMIT_ACTION == "ingestion_run.submit"
    assert audit_actions.INGESTION_RUN_COMPLETE_ACTION == "ingestion_run.complete"
    assert audit_actions.INGESTION_RUN_RESOURCE_TYPE == "ingestion_run"
    assert audit_actions.INGESTION_RUN_RECONCILER_ACTOR == "system:ingestion-run-reconciler"


def test_submission_entry_is_applied_and_started_and_validates() -> None:
    entry = audit_actions.submission_audit_entry(celex="32024R2847", short_name="cra")

    assert entry.action == "ingestion_run.submit"
    assert entry.outcome == "applied"
    assert entry.details == {"celex": "32024R2847", "short_name": "cra", "status": "started"}
    audit_actions.IngestionRunSubmitDetails.model_validate(entry.details)


def test_a_succeeded_completion_is_applied_and_carries_instrument_id_and_outcome() -> None:
    entry = audit_actions.completion_audit_entry(status="succeeded", result=_RESULT, error=None)

    assert entry.action == "ingestion_run.complete"
    assert entry.outcome == "applied"
    assert entry.details == {
        "status": "succeeded",
        "regulatory_instrument_id": "cra-1.0",
        "outcome": "fresh",
    }
    audit_actions.IngestionRunCompleteDetails.model_validate(entry.details)


def test_a_failed_completion_is_failed_and_carries_the_error_text() -> None:
    entry = audit_actions.completion_audit_entry(
        status="failed", result=None, error="error: extraction stage failed: boom"
    )

    assert entry.outcome == "failed"
    assert entry.details == {"status": "failed", "error": "error: extraction stage failed: boom"}
    audit_actions.IngestionRunCompleteDetails.model_validate(entry.details)


def test_a_succeeded_completion_without_the_expected_result_keys_still_validates() -> None:
    entry = audit_actions.completion_audit_entry(status="succeeded", result={}, error=None)

    assert entry.details == {"status": "succeeded"}
    audit_actions.IngestionRunCompleteDetails.model_validate(entry.details)


@pytest.mark.parametrize(
    ("model", "payload"),
    [
        (
            audit_actions.IngestionRunSubmitDetails,
            {"celex": "c", "short_name": "s", "status": "started", "token": "x"},
        ),
        (
            audit_actions.IngestionRunCompleteDetails,
            {"status": "succeeded", "stages": []},
        ),
    ],
)
def test_details_reject_undeclared_fields(
    model: type[AuditDetails], payload: dict[str, object]
) -> None:
    with pytest.raises(ValidationError):
        model.model_validate(payload)


def test_details_reject_a_status_outside_the_declared_literals() -> None:
    with pytest.raises(ValidationError):
        audit_actions.IngestionRunSubmitDetails.model_validate(
            {"celex": "c", "short_name": "s", "status": "running"}
        )
    with pytest.raises(ValidationError):
        audit_actions.IngestionRunCompleteDetails.model_validate({"status": "started"})
