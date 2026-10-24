"""Authentication comes before path-identifier validation on the identifier routes.

An unauthenticated caller gets the same 401 whether the identifier in the path is well formed or
not, so the 422 for a malformed identifier is never reachable without a verified caller. Drives the
real composition root against the mock OIDC provider, like ``test_rest_auth_middleware.py``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient

from ps_service.config import ServiceConfig
from ps_service.main import create_app

# Registers pytest's "mock_oidc_provider" fixture; each test below declares that name as a
# parameter instead of calling the import.
from ps_test_support.mock_oidc_provider import (
    mock_oidc_provider_fixture,  # noqa: F401  # pyright: ignore[reportUnusedImport]
)

if TYPE_CHECKING:
    from pathlib import Path

    from ps_test_support.mock_oidc_provider import MockOidcProvider


@pytest.fixture(autouse=True)
def _configure_logging(  # pyright: ignore[reportUnusedFunction]  # pytest autouse fixture — invoked by name-collection, never referenced in-module
    configured_logging: Path,
) -> None:
    """Install a process-wide logging facade so the token verifier can log."""


def _unauthenticated_client(provider: MockOidcProvider) -> TestClient:
    config = ServiceConfig(
        host="127.0.0.1",
        port=8000,
        graceful_shutdown_seconds=10,
        logging_dir=None,
        is_local_test_bypass_active=False,
        auth_issuer=provider.issuer,
        auth_audience="ps-service",
        authz_bootstrap_owner_subject="first-owner-subject",
        authz_bootstrap_owner_issuer=provider.issuer,
        authentik_api_token="test-authentik-token",
        authentik_base_url="https://authentik.example.com",
        public_url="https://ps.example.com:30443",
    )
    return TestClient(create_app(config))


def test_unauthenticated_ingestion_status_read_gets_the_same_401_for_a_malformed_run_id(
    mock_oidc_provider: MockOidcProvider,
) -> None:
    client = _unauthenticated_client(mock_oidc_provider)

    valid = client.get("/ingestions/run-1")
    malformed = client.get("/ingestions/run.1")

    assert valid.status_code == 401
    assert malformed.status_code == valid.status_code
    assert malformed.json() == valid.json()


def test_unauthenticated_merge_resolution_gets_the_same_401_for_a_malformed_review_id(
    mock_oidc_provider: MockOidcProvider,
) -> None:
    client = _unauthenticated_client(mock_oidc_provider)
    body = {"decision": "keep-separate"}

    valid = client.post("/near-misses/review_aaa/resolve", json=body)
    malformed = client.post("/near-misses/review.aaa/resolve", json=body)

    assert valid.status_code == 401
    assert malformed.status_code == valid.status_code
    assert malformed.json() == valid.json()
