"""`instrument.restore` typed audit details and failure classification."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from ps_service.api.errors import (
    GraphWriteUnavailableError,
    RestoreArtifactRejectedError,
    RestoreStageFailedError,
)
from ps_service.audit import is_known_resource_type, resolve_details_model
from ps_service.mcp_interface.errors import McpGraphUnavailableError
from ps_service.restore.audit_actions import (
    INSTRUMENT_RESTORE_ACTION,
    InstrumentRestoreDetails,
    classify_restore_failure_reason,
    classify_restore_terminal_row,
)
from ps_service.restore.errors import (
    RestoreApplyPendingError,
    RestoreGraphWriteFailedError,
    RestoreGroupRejectedError,
)


def test_instrument_restore_registered_with_typed_details() -> None:
    assert INSTRUMENT_RESTORE_ACTION == "instrument.restore"
    assert resolve_details_model("instrument.restore") is InstrumentRestoreDetails


@pytest.mark.parametrize("field", ["source_url", "url", "traceback", "error", "path"])
def test_instrument_restore_details_reject_unknown_fields(field: str) -> None:
    with pytest.raises(ValidationError):
        InstrumentRestoreDetails.model_validate(
            {"instrument_id": "CRA-1.0", "status": "started", "source": "catalog", field: "x"}
        )


def test_instrument_restore_details_carry_instrument_id_status_source_and_reason_code() -> None:
    assert set(InstrumentRestoreDetails.model_fields) == {
        "instrument_id",
        "status",
        "source",
        "reason_code",
    }
    details = InstrumentRestoreDetails(
        instrument_id="CRA-1.0", status="failed", source="upload", reason_code="artifact_rejected"
    )
    assert details.reason_code == "artifact_rejected"
    with pytest.raises(ValidationError):
        InstrumentRestoreDetails.model_validate(
            {"instrument_id": "X", "status": "failed", "source": "catalog", "reason_code": "boom"}
        )
    with pytest.raises(ValidationError):
        InstrumentRestoreDetails.model_validate(
            {"instrument_id": "X", "status": "weird", "source": "catalog"}
        )


def test_instrument_resource_type_is_registered() -> None:
    assert is_known_resource_type("instrument")


class _Unrelated(Exception):  # noqa: N818
    pass


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (RestoreArtifactRejectedError("bad checksum"), "artifact_rejected"),
        (RestoreStageFailedError(stage="configuration", reason="x"), "config_incomplete"),
        (RestoreStageFailedError(stage="content_validation", reason="x"), "content_rejected"),
        (RestoreStageFailedError(stage="concurrency", reason="x"), "concurrent_restore"),
        (RestoreStageFailedError(stage="restore", reason="x"), "restore_failed"),
        (McpGraphUnavailableError(), "graph_unavailable"),
        (GraphWriteUnavailableError("no gateway"), "graph_unavailable"),
        (_Unrelated("secret detail"), "unexpected_error"),
        (ValueError("boom"), "unexpected_error"),
    ],
)
def test_restore_reason_code_for_each_failure_class(exc: Exception, expected: str) -> None:
    assert classify_restore_failure_reason(exc) == expected


def _stage_failure_from(cause: Exception) -> RestoreStageFailedError:
    """What the orchestration raises for a `graph_write` stage failure (chained from `cause`)."""
    failure = RestoreStageFailedError(stage="graph_write", reason="graph_write failed")
    failure.__cause__ = cause
    return failure


@pytest.mark.parametrize("status", ["rejected", "committed_apply_pending"])
def test_the_two_graph_write_statuses_are_valid_details(status: str) -> None:
    details = InstrumentRestoreDetails.model_validate(
        {"instrument_id": "CRA-1.0", "status": status, "source": "upload"}
    )
    assert details.status == status


def test_group_rejected_is_a_valid_reason_code() -> None:
    details = InstrumentRestoreDetails.model_validate(
        {
            "instrument_id": "CRA-1.0",
            "status": "rejected",
            "source": "catalog",
            "reason_code": "group_rejected",
        }
    )
    assert details.reason_code == "group_rejected"


@pytest.mark.parametrize(
    ("cause", "reason"),
    [
        (RestoreGroupRejectedError("refused"), "group_rejected"),
        (RestoreGraphWriteFailedError("down"), "graph_unavailable"),
        (RestoreApplyPendingError("committed"), "restore_failed"),
    ],
)
def test_a_graph_write_stage_failure_is_classified_by_the_failure_it_was_raised_from(
    cause: Exception, reason: str
) -> None:
    assert classify_restore_failure_reason(_stage_failure_from(cause)) == reason


@pytest.mark.parametrize(
    ("exc", "outcome", "status", "reason"),
    [
        (
            _stage_failure_from(RestoreGroupRejectedError("refused")),
            "rejected",
            "rejected",
            "group_rejected",
        ),
        (
            _stage_failure_from(RestoreApplyPendingError("committed")),
            "applied",
            "committed_apply_pending",
            None,
        ),
        (
            _stage_failure_from(RestoreGraphWriteFailedError("down")),
            "failed",
            "failed",
            "graph_unavailable",
        ),
        (
            RestoreStageFailedError(stage="concurrency", reason="x"),
            "failed",
            "failed",
            "concurrent_restore",
        ),
        (RestoreArtifactRejectedError("bad"), "failed", "failed", "artifact_rejected"),
        (GraphWriteUnavailableError("no gateway"), "failed", "failed", "graph_unavailable"),
    ],
)
def test_the_terminal_row_of_each_restore_outcome(
    exc: Exception, outcome: str, status: str, reason: str | None
) -> None:
    row = classify_restore_terminal_row(exc)

    assert (row.outcome, row.status, row.reason_code) == (outcome, status, reason)
