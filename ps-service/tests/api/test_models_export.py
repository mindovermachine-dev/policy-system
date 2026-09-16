"""Unit tests for the ``POST /exports`` request/response models (`ps_service.api.models`)
and the new export error types (`ps_service.api.errors`).

Mirrors ``test_models_restoration.py``'s pure-Pydantic-level style: no FastAPI
``TestClient``, no FalkorDB (issue #71 PLAN.md S1, CHANGES.md Appendix A2).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from ps_service.api.errors import (
    ApiError,
    ExportConfigIncompleteError,
    ExportInstrumentNotFoundError,
    ExportStageFailedError,
)
from ps_service.api.models import (
    ExportAcceptedResponse,
    ExportManifestPayload,
    ExportRequest,
    ExportStageOutcome,
)

_MANIFEST_PAYLOAD: dict[str, object] = {
    "instrument_id": "CRA-1.0",
    "celex": "32024R2847",
    "title": "Cyber Resilience Act",
    "short_name": "CRA",
    "version": "1.0",
    "source_type": "external",
    "jurisdiction": "EU",
    "schema_version": "1",
    "exported_at": "2026-01-01T00:00:00Z",
    "baseline_sha256": "a" * 64,
    "native_sha256": "b" * 64,
}


# --- ExportRequest -----------------------------------------------------------


def test_export_request_accepts_a_well_formed_instrument_id() -> None:
    request = ExportRequest.model_validate({"instrument_id": "CRA-1.0"})

    assert request.instrument_id == "CRA-1.0"


def test_export_request_rejects_missing_instrument_id() -> None:
    with pytest.raises(ValidationError):
        ExportRequest.model_validate({})


def test_export_request_rejects_empty_instrument_id() -> None:
    with pytest.raises(ValidationError):
        ExportRequest.model_validate({"instrument_id": ""})


def test_export_request_rejects_instrument_id_outside_the_pattern() -> None:
    with pytest.raises(ValidationError):
        ExportRequest.model_validate({"instrument_id": "not a valid id!"})


def test_export_request_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        ExportRequest.model_validate({"instrument_id": "CRA-1.0", "unexpected": "nope"})


# --- ExportManifestPayload ----------------------------------------------------


def test_export_manifest_payload_accepts_the_same_eleven_field_shape_as_restoration() -> None:
    """Field-for-field parity with `RestorationManifestPayload`'s own fixture shape."""
    payload = ExportManifestPayload.model_validate(_MANIFEST_PAYLOAD)

    assert payload.instrument_id == "CRA-1.0"
    assert payload.celex == "32024R2847"
    assert payload.title == "Cyber Resilience Act"
    assert payload.short_name == "CRA"
    assert payload.version == "1.0"
    assert payload.source_type == "external"
    assert payload.jurisdiction == "EU"
    assert payload.schema_version == "1"
    assert payload.exported_at == "2026-01-01T00:00:00Z"
    assert payload.baseline_sha256 == "a" * 64
    assert payload.native_sha256 == "b" * 64


def test_export_manifest_payload_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        ExportManifestPayload.model_validate({**_MANIFEST_PAYLOAD, "unexpected": "nope"})


# --- ExportAcceptedResponse ----------------------------------------------------


def test_export_accepted_response_round_trips_every_field() -> None:
    body = {
        "instrument_id": "CRA-1.0",
        "manifest": _MANIFEST_PAYLOAD,
        "baseline_blob_base64": "eyJub2RlcyI6IFtdfQ==",
        "native_blob_base64": "eyJub2RlcyI6IFtdfQ==",
        "stages": [{"stage": "embedded", "status": "succeeded"}],
    }

    response = ExportAcceptedResponse.model_validate(body)

    assert response.instrument_id == "CRA-1.0"
    assert response.manifest.celex == "32024R2847"
    assert response.baseline_blob_base64 == body["baseline_blob_base64"]
    assert response.native_blob_base64 == body["native_blob_base64"]
    assert response.stages == [ExportStageOutcome(stage="embedded", status="succeeded")]


def test_export_accepted_response_is_frozen() -> None:
    response = ExportAcceptedResponse(
        instrument_id="CRA-1.0",
        manifest=ExportManifestPayload.model_validate(_MANIFEST_PAYLOAD),
        baseline_blob_base64="eyJub2RlcyI6IFtdfQ==",
        native_blob_base64="eyJub2RlcyI6IFtdfQ==",
        stages=[ExportStageOutcome(stage="embedded", status="succeeded")],
    )

    with pytest.raises(ValidationError):
        response.instrument_id = "other"  # type: ignore[misc]


# --- error types ---------------------------------------------------------------


@pytest.mark.parametrize(
    "exc",
    [
        ExportInstrumentNotFoundError("CRA-1.0 not found"),
        ExportConfigIncompleteError("PS_LLMINTERFACE_EMBED_MODEL is not set"),
        ExportStageFailedError(stage="x", reason="y"),
    ],
)
def test_export_error_types_are_api_errors(exc: Exception) -> None:
    assert isinstance(exc, ApiError)


def test_export_stage_failed_error_exposes_stage_and_reason() -> None:
    exc = ExportStageFailedError(stage="x", reason="y")

    assert exc.stage == "x"
    assert exc.reason == "y"
    assert str(exc) == "x stage failed: y"
