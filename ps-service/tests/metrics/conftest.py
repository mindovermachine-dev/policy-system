"""Fixtures for `ps_service.metrics` tests."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from api.conftest import (
    configured_logging,  # noqa: F401  # pyright: ignore[reportUnusedImport]  # pytest discovers the fixture by name
)

from metrics._app import fake_discovery_document

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture(autouse=True)
def _stub_auth_discovery(  # pyright: ignore[reportUnusedFunction]  # pytest autouse fixture — invoked by name-collection, never referenced in-module
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fake only the OIDC discovery fetch, the one real network boundary under `create_app`."""
    monkeypatch.setattr("ps_service.auth.startup.fetch_discovery_document", fake_discovery_document)


@pytest.fixture(autouse=True)
def _use_configured_logging(  # pyright: ignore[reportUnusedFunction]  # pytest autouse fixture — invoked by name-collection, never referenced in-module
    configured_logging: Path,  # noqa: F811  # the imported fixture is requested by parameter name
) -> None:
    """Run every test with a real default emitter, as the service has after startup.

    Requests answered by the authentication middleware go through the structured error
    handlers, which log through the process-wide facade.
    """
