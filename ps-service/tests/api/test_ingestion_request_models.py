"""Unit tests for the ``POST /ingestions`` request models (`ps_service.api.models`).

Pure Pydantic-level checks: the discriminated union resolves the right member by
``source``, and the CELEX/``short_name`` constraints reject malformed identifiers
(AC-BI-001/005/006).
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

_VALID_CELEX = "32024R2847"
_VALID_SHORT_NAME = "cra"


def test_catalog_request_rejects_malformed_celex() -> None:
    """AC-BI-006: a CELEX that violates the curated ``3ddddXdddd`` shape is rejected."""
    with pytest.raises(ValidationError):
        CatalogIngestionRequest.model_validate(
            {"source": "catalog", "celex": "not-a-celex", "short_name": _VALID_SHORT_NAME}
        )

    valid = CatalogIngestionRequest.model_validate(
        {"source": "catalog", "celex": _VALID_CELEX, "short_name": _VALID_SHORT_NAME}
    )
    assert valid.celex == _VALID_CELEX


def test_catalog_ingestion_request_requires_short_name() -> None:
    """AC-BI-001: ``short_name`` is required -- omitting it fails validation before any
    pipeline dependency is ever touched.
    """
    with pytest.raises(ValidationError):
        CatalogIngestionRequest.model_validate({"source": "catalog", "celex": _VALID_CELEX})


@pytest.mark.parametrize(
    "short_name",
    [
        "1bad",  # starts with a digit, not a letter
        "",  # empty
        "a" * 65,  # exceeds the 64-char bound
    ],
)
def test_catalog_ingestion_request_rejects_malformed_short_name(short_name: str) -> None:
    """AC-BI-005: a ``short_name`` violating the shared pattern is rejected."""
    with pytest.raises(ValidationError):
        CatalogIngestionRequest.model_validate(
            {"source": "catalog", "celex": _VALID_CELEX, "short_name": short_name}
        )


def test_catalog_ingestion_request_accepts_valid_short_name() -> None:
    """AC-BI-005: a well-formed ``short_name`` round-trips unchanged."""
    valid = CatalogIngestionRequest.model_validate(
        {"source": "catalog", "celex": _VALID_CELEX, "short_name": _VALID_SHORT_NAME}
    )
    assert valid.short_name == _VALID_SHORT_NAME


def test_catalog_ingestion_request_accepts_optional_run_id() -> None:
    """AC-BI-008 correlation: an omitted ``run_id`` defaults to ``None`` (auto-mint
    fallback at the route layer); a well-formed one round-trips unchanged.
    """
    without = CatalogIngestionRequest.model_validate(
        {"source": "catalog", "celex": _VALID_CELEX, "short_name": _VALID_SHORT_NAME}
    )
    assert without.run_id is None

    with_run_id = CatalogIngestionRequest.model_validate(
        {
            "source": "catalog",
            "celex": _VALID_CELEX,
            "short_name": _VALID_SHORT_NAME,
            "run_id": "client-abc123",
        }
    )
    assert with_run_id.run_id == "client-abc123"


def test_catalog_ingestion_request_rejects_malformed_run_id() -> None:
    """AC-BI-008: a ``run_id`` containing ``/`` fails the path-safe pattern constraint."""
    with pytest.raises(ValidationError):
        CatalogIngestionRequest.model_validate(
            {
                "source": "catalog",
                "celex": _VALID_CELEX,
                "short_name": _VALID_SHORT_NAME,
                "run_id": "not/safe",
            }
        )


def test_discriminator_selects_catalog_vs_internal_model() -> None:
    """AC-BI-006: ``source`` routes the body to the matching union member."""
    catalog = _ADAPTER.validate_python(
        {"source": "catalog", "celex": "32016R0679", "short_name": "gdpr"}
    )
    internal = _ADAPTER.validate_python(
        {"source": "internal", "content": {"nodes": [], "edges": []}}
    )

    assert isinstance(catalog, CatalogIngestionRequest)
    assert isinstance(internal, InternalIngestionRequest)

    with pytest.raises(ValidationError):
        _ADAPTER.validate_python({"source": "unknown"})
