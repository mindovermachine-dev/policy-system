"""Unit tests for the ``POST /restorations`` request/response models (`ps_service.api.models`).

Mirrors ``test_ingestion_request_models.py``'s pure-Pydantic-level style: no
FastAPI ``TestClient``, no FalkorDB. Covers ``RestorationRequest``'s
``extra="forbid"``/``Field(min_length=...)`` shape (mirroring
``CatalogIngestionRequest``'s own conventions) and the frozen response models.
"""

from __future__ import annotations

import base64

import pytest
from pydantic import ValidationError

from ps_service.api.models import (
    RestorationAcceptedResponse,
    RestorationRequest,
    RestorationStageOutcome,
)

_VALID_BODY: dict[str, object] = {
    "instrument_id": "CRA-1.0",
    "manifest": {
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
    },
    "baseline_blob_base64": base64.b64encode(b"{}").decode("ascii"),
    "native_blob_base64": base64.b64encode(b"{}").decode("ascii"),
}


def test_restoration_request_accepts_a_well_formed_body() -> None:
    """A well-formed body validates and round-trips every field unchanged."""
    request = RestorationRequest.model_validate(_VALID_BODY)

    assert request.instrument_id == "CRA-1.0"
    assert request.manifest.celex == "32024R2847"
    assert request.baseline_blob_base64 == _VALID_BODY["baseline_blob_base64"]


def test_restoration_request_rejects_unknown_fields() -> None:
    """``extra="forbid"`` mirrors ``CatalogIngestionRequest``'s own convention."""
    with pytest.raises(ValidationError):
        RestorationRequest.model_validate({**_VALID_BODY, "unexpected": "nope"})


def test_restoration_request_rejects_empty_instrument_id() -> None:
    """``instrument_id`` carries a ``Field(min_length=1)`` constraint."""
    with pytest.raises(ValidationError):
        RestorationRequest.model_validate({**_VALID_BODY, "instrument_id": ""})


_NOT_STRICT_BASE64 = [
    pytest.param("not-valid-base64!!!", id="bad-alphabet"),
    pytest.param("YQ", id="missing-padding"),
    pytest.param("YQ=", id="short-padding"),
    pytest.param("YQ==\n", id="trailing-newline"),
    pytest.param("eyJu b2Rlcy", id="embedded-space"),
    pytest.param("@@@@", id="symbols-only"),
]


@pytest.mark.parametrize("field", ["baseline_blob_base64", "native_blob_base64"])
@pytest.mark.parametrize("blob", _NOT_STRICT_BASE64)
def test_restoration_request_rejects_a_blob_that_is_not_strict_base64(
    field: str, blob: str
) -> None:
    with pytest.raises(ValidationError) as excinfo:
        RestorationRequest.model_validate({**_VALID_BODY, field: blob})

    assert [error["loc"] for error in excinfo.value.errors()] == [(field,)]
    assert blob not in str(excinfo.value)


def test_restoration_request_error_text_never_contains_a_submitted_blob() -> None:
    marker = "marker-blob-value!!!"

    with pytest.raises(ValidationError) as excinfo:
        RestorationRequest.model_validate({**_VALID_BODY, "baseline_blob_base64": marker})

    assert marker not in str(excinfo.value)


def test_restoration_request_accepts_blobs_that_are_strict_base64() -> None:
    blob = base64.b64encode(b'{"nodes": [], "edges": []}').decode("ascii")

    request = RestorationRequest.model_validate(
        {**_VALID_BODY, "baseline_blob_base64": blob, "native_blob_base64": blob}
    )

    assert request.baseline_blob_base64 == blob
    assert request.native_blob_base64 == blob


def test_restoration_stage_outcome_and_accepted_response_are_frozen() -> None:
    """Response models are frozen, mirroring ``StageOutcome``/``IngestionAcceptedResponse``."""
    stage = RestorationStageOutcome(stage="verified", status="succeeded")
    response = RestorationAcceptedResponse(
        instrument_id="CRA-1.0",
        stages=[stage],
        near_miss_reviews_created=0,
    )

    with pytest.raises(ValidationError):
        response.instrument_id = "other"  # type: ignore[misc]

    assert response.stages[0].stage == "verified"


def test_accepted_response_requires_a_non_negative_near_miss_review_count() -> None:
    stage = RestorationStageOutcome(stage="verified", status="succeeded")

    with pytest.raises(ValidationError):
        RestorationAcceptedResponse.model_validate({"instrument_id": "CRA-1.0", "stages": [stage]})
    with pytest.raises(ValidationError):
        RestorationAcceptedResponse(
            instrument_id="CRA-1.0", stages=[stage], near_miss_reviews_created=-1
        )
    response = RestorationAcceptedResponse(
        instrument_id="CRA-1.0", stages=[stage], near_miss_reviews_created=3
    )
    assert response.model_dump()["near_miss_reviews_created"] == 3
