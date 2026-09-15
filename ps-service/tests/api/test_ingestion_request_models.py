"""Unit tests for the ``POST /ingestions`` request models (`ps_service.api.models`).

Pure Pydantic-level checks: the discriminated union resolves the right member by
``source``, and the CELEX constraint rejects malformed identifiers (AC-BI-006).
No FastAPI ``TestClient`` is involved.
"""

from __future__ import annotations

import pytest
from pydantic import TypeAdapter, ValidationError

from ps_service.api.models import (
    CatalogIngestionRequest,
    IngestionRequest,
    InternalIngestionRequest,
)

_ADAPTER: TypeAdapter[CatalogIngestionRequest | InternalIngestionRequest] = TypeAdapter(
    IngestionRequest
)


def test_catalog_request_rejects_malformed_celex() -> None:
    """AC-BI-006: a CELEX that violates the curated ``3ddddXdddd`` shape is rejected."""
    with pytest.raises(ValidationError):
        CatalogIngestionRequest.model_validate({"source": "catalog", "celex": "not-a-celex"})

    valid = CatalogIngestionRequest.model_validate({"source": "catalog", "celex": "32024R2847"})
    assert valid.celex == "32024R2847"


def test_catalog_ingestion_request_accepts_optional_run_id() -> None:
    """AC-BI-008 correlation: an omitted ``run_id`` defaults to ``None`` (auto-mint
    fallback at the route layer); a well-formed one round-trips unchanged.
    """
    without = CatalogIngestionRequest.model_validate({"source": "catalog", "celex": "32024R2847"})
    assert without.run_id is None

    with_run_id = CatalogIngestionRequest.model_validate(
        {"source": "catalog", "celex": "32024R2847", "run_id": "client-abc123"}
    )
    assert with_run_id.run_id == "client-abc123"


def test_catalog_ingestion_request_rejects_malformed_run_id() -> None:
    """AC-BI-008: a ``run_id`` containing ``/`` fails the path-safe pattern constraint."""
    with pytest.raises(ValidationError):
        CatalogIngestionRequest.model_validate(
            {"source": "catalog", "celex": "32024R2847", "run_id": "not/safe"}
        )


def test_discriminator_selects_catalog_vs_internal_model() -> None:
    """AC-BI-006: ``source`` routes the body to the matching union member."""
    catalog = _ADAPTER.validate_python({"source": "catalog", "celex": "32016R0679"})
    internal = _ADAPTER.validate_python(
        {"source": "internal", "content": {"nodes": [], "edges": []}}
    )

    assert isinstance(catalog, CatalogIngestionRequest)
    assert isinstance(internal, InternalIngestionRequest)

    with pytest.raises(ValidationError):
        _ADAPTER.validate_python({"source": "unknown"})
