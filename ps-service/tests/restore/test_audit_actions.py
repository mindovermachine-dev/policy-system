"""`instrument.restore` typed audit details and failure classification (issue #195, Slice 4)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from ps_service.api.errors import RestoreArtifactRejectedError, RestoreStageFailedError
from ps_service.audit import is_known_resource_type, resolve_details_model
from ps_service.mcp_interface.errors import McpGraphUnavailableError
from ps_service.restore.audit_actions import (
    INSTRUMENT_RESTORE_ACTION,
    InstrumentRestoreDetails,
    classify_restore_failure_reason,
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
        (_Unrelated("secret detail"), "unexpected_error"),
        (ValueError("boom"), "unexpected_error"),
    ],
)
def test_restore_reason_code_for_each_failure_class(exc: Exception, expected: str) -> None:
    assert classify_restore_failure_reason(exc) == expected
