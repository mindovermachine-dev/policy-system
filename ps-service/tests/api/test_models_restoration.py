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


def test_restoration_stage_outcome_and_accepted_response_are_frozen() -> None:
    """Response models are frozen, mirroring ``StageOutcome``/``IngestionAcceptedResponse``."""
    stage = RestorationStageOutcome(stage="verified", status="succeeded")
    response = RestorationAcceptedResponse(
        instrument_id="CRA-1.0",
        stages=[stage],
    )

    with pytest.raises(ValidationError):
        response.instrument_id = "other"  # type: ignore[misc]

    assert response.stages[0].stage == "verified"
