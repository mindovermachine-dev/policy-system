"""Tests for the ingestion-run audit actions' registration, typed details and builders (issue #194).

AC-BI-016 / AC-BI-017. `audit_events.outcome` is constrained to `applied|rejected|failed`, so
"outcome=started" is stored as `outcome="applied"` plus `details.status="started"` (OQ-2).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from ps_service.audit import AuditDetails, is_known_resource_type, resolve_details_model
from ps_service.ingestion_runs import audit_actions

_CELEX = "32024R2847"
_RESULT: dict[str, object] = {
    "run_id": "r",
    "regulatory_instrument_id": "cra-1.0",
    "source": "catalog",
    "outcome": "fresh",
    "stages": [
        {"stage": "ingestion", "status": "succeeded", "summary": {"verified_labels": 3}},
        {
            "stage": "merge",
            "status": "succeeded",
            "summary": {
                "obligations": 9,
                "canonical_capabilities": 5,
                "new_obligations": 7,
                "new_capabilities": 2,
                "matched_capabilities": 3,
            },
        },
    ],
}


def _valid_complete() -> dict[str, object]:
    return {
        "status": "succeeded",
        "celex": _CELEX,
        "trigger": "sync_ingest",
        "new_obligations": 0,
        "new_capabilities": 0,
        "matched_capabilities": 0,
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


def test_submit_details_require_trigger_and_accept_only_enumerated_values() -> None:
    entry = audit_actions.submission_audit_entry(
        celex=_CELEX, short_name="cra", trigger="async_ingest"
    )

    assert entry.action == "ingestion_run.submit"
    assert entry.outcome == "applied"
    assert entry.details == {
        "celex": _CELEX,
        "short_name": "cra",
        "status": "started",
        "trigger": "async_ingest",
    }
    audit_actions.IngestionRunSubmitDetails.model_validate(entry.details)
    with pytest.raises(ValidationError):
        audit_actions.IngestionRunSubmitDetails.model_validate(
            {"celex": _CELEX, "short_name": "cra", "status": "started"}
        )
    with pytest.raises(ValidationError):
        audit_actions.IngestionRunSubmitDetails.model_validate(
            {"celex": _CELEX, "short_name": "cra", "status": "started", "trigger": "cron"}
        )


@pytest.mark.parametrize("trigger", ["sync_ingest", "async_ingest", "amendment_check"])
def test_every_documented_trigger_is_accepted(trigger: str) -> None:
    audit_actions.IngestionRunSubmitDetails.model_validate(
        {"celex": _CELEX, "short_name": "cra", "status": "started", "trigger": trigger}
    )


def test_complete_details_carry_trigger_celex_instrument_id_outcome_and_three_counts() -> None:
    entry = audit_actions.completion_audit_entry(
        status="succeeded",
        celex=_CELEX,
        trigger="sync_ingest",
        result=_RESULT,
        reason_code=None,
    )

    assert entry.action == "ingestion_run.complete"
    assert entry.outcome == "applied"
    assert entry.details == {
        "status": "succeeded",
        "celex": _CELEX,
        "trigger": "sync_ingest",
        "regulatory_instrument_id": "cra-1.0",
        "outcome": "fresh",
        "new_obligations": 7,
        "new_capabilities": 2,
        "matched_capabilities": 3,
    }
    audit_actions.IngestionRunCompleteDetails.model_validate(entry.details)


def test_complete_details_have_no_free_text_error_field() -> None:
    assert "error" not in audit_actions.IngestionRunCompleteDetails.model_fields
    with pytest.raises(ValidationError):
        audit_actions.IngestionRunCompleteDetails.model_validate(
            {**_valid_complete(), "error": "error: boom"}
        )


def test_complete_failed_details_require_an_enumerated_reason_code() -> None:
    failed = {**_valid_complete(), "status": "failed"}
    with pytest.raises(ValidationError):
        audit_actions.IngestionRunCompleteDetails.model_validate(failed)
    with pytest.raises(ValidationError):
        audit_actions.IngestionRunCompleteDetails.model_validate(
            {**failed, "reason_code": "free text"}
        )
    audit_actions.IngestionRunCompleteDetails.model_validate(
        {**failed, "reason_code": "pipeline_stage_failed"}
    )
    with pytest.raises(ValidationError):  # a reason on a succeeded run is a contradiction
        audit_actions.IngestionRunCompleteDetails.model_validate(
            {**_valid_complete(), "reason_code": "interrupted"}
        )


def test_complete_details_counts_are_required_non_negative_ints() -> None:
    incomplete = _valid_complete()
    del incomplete["new_capabilities"]
    with pytest.raises(ValidationError):
        audit_actions.IngestionRunCompleteDetails.model_validate(incomplete)
    with pytest.raises(ValidationError):
        audit_actions.IngestionRunCompleteDetails.model_validate(
            {**_valid_complete(), "new_obligations": -1}
        )


def test_completion_entry_reads_counts_from_the_merge_stage_summary_of_the_result() -> None:
    counts = audit_actions.IngestionCounts.from_result(_RESULT)

    assert (counts.new_obligations, counts.new_capabilities, counts.matched_capabilities) == (
        7,
        2,
        3,
    )


def test_completion_entry_for_already_ingested_has_zero_counts() -> None:
    entry = audit_actions.completion_audit_entry(
        status="succeeded",
        celex=_CELEX,
        trigger="sync_ingest",
        result={
            "regulatory_instrument_id": "cra-1.0",
            "outcome": "already_ingested",
            "stages": [],
        },
        reason_code=None,
    )

    assert entry.details["outcome"] == "already_ingested"
    assert (
        entry.details["new_obligations"],
        entry.details["new_capabilities"],
        entry.details["matched_capabilities"],
    ) == (0, 0, 0)


def test_completion_entry_for_interrupted_run_has_reason_code_interrupted() -> None:
    entry = audit_actions.completion_audit_entry(
        status="failed",
        celex=_CELEX,
        trigger="async_ingest",
        result=None,
        reason_code="interrupted",
    )

    assert entry.outcome == "failed"
    assert entry.details["reason_code"] == "interrupted"
    counts = (
        entry.details["new_obligations"],
        entry.details["new_capabilities"],
        entry.details["matched_capabilities"],
    )
    assert counts == (0, 0, 0)
    assert entry.details["celex"] == _CELEX
    audit_actions.IngestionRunCompleteDetails.model_validate(entry.details)


def test_completion_entry_never_contains_stage_error_text() -> None:
    entry = audit_actions.completion_audit_entry(
        status="failed",
        celex=_CELEX,
        trigger="sync_ingest",
        result=None,
        reason_code="pipeline_stage_failed",
    )

    assert "error" not in entry.details
    assert set(entry.details) == {
        "status",
        "celex",
        "trigger",
        "reason_code",
        "new_obligations",
        "new_capabilities",
        "matched_capabilities",
    }


def test_counts_from_a_result_without_a_merge_stage_are_zero() -> None:
    assert audit_actions.IngestionCounts.from_result(None) == audit_actions.IngestionCounts(0, 0, 0)
    assert audit_actions.IngestionCounts.from_result({"stages": [{"stage": "ingestion"}]}) == (
        audit_actions.IngestionCounts(0, 0, 0)
    )


@pytest.mark.parametrize(
    ("model", "payload"),
    [
        (
            audit_actions.IngestionRunSubmitDetails,
            {
                "celex": "c",
                "short_name": "s",
                "status": "started",
                "trigger": "sync_ingest",
                "x": 1,
            },
        ),
        (
            audit_actions.IngestionRunCompleteDetails,
            {**_valid_complete(), "stages": []},
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
            {"celex": "c", "short_name": "s", "status": "running", "trigger": "sync_ingest"}
        )
    with pytest.raises(ValidationError):
        audit_actions.IngestionRunCompleteDetails.model_validate({"status": "started"})
