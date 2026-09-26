"""HTTP tests for `GET /catalog` (issue #125's runtime curated-content fetch).

Every test in this file injects a fake HTTP transport
(`build_fake_curated_catalog_dependencies`, `tests/api/_fakes.py`) wired into
the *real* `ps_service.curated_source.catalog_client.fetch_catalog` -- never
a canned-entries stand-in and never real network (BASELINE's hermetic-suite
requirement). `_configure_logging_for_catalog_tests` installs a real,
per-test Logging facade (mirrors `test_rest_auth_middleware.py`'s own
`_configure_logging_for_auth_tests` fixture) because `fetch_catalog` now
emits a structured log entry on every call, and the shared `client` fixture
(`tests/api/conftest.py`) deliberately never enters `lifespan`/`configure()`.
"""

from __future__ import annotations

import dataclasses
import json
from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient

from api._fakes import (
    FakeCuratedSourceTransport,
    FakeFailingCuratedSourceTransport,
    build_fake_curated_catalog_dependencies,
)
from ps_service.api.dependencies import provide_curated_catalog_dependencies
from ps_service.main import create_app

if TYPE_CHECKING:
    from pathlib import Path

    from ps_service.config import ServiceConfig
    from ps_service.curated_source.http_fetch import CuratedSourceTransport

_CUSTOM_URL = "https://example.com/operator-configured-catalog"

_CANNED_ENTRIES = [
    {
        "instrument_id": "CRA-1.0",
        "celex": "32024R2847",
        "title": "Cyber Resilience Act",
        "source_type": "external",
        "jurisdiction": "EU",
        "short_name": "CRA",
        "version": "1.0",
    },
    {
        "instrument_id": "ENGPRAC-2.1",
        "celex": None,
        "title": "Engineering Practices",
        "source_type": "internal",
        "jurisdiction": None,
        "short_name": "ENGPRAC",
        "version": "2.1",
    },
]


@pytest.fixture(autouse=True)
def _configure_logging_for_catalog_tests(  # pyright: ignore[reportUnusedFunction]  # pytest autouse fixture — invoked by name-collection, never referenced in-module
    configured_logging: Path,
) -> None:
    """`fetch_catalog` always logs (issue #125) -- install a real Logging facade.

    Mirrors `test_rest_auth_middleware.py`'s own
    `_configure_logging_for_auth_tests` fixture, for the same reason: the
    shared bare `client` fixture never enters `lifespan`, so nothing else in
    this file would otherwise configure the process-wide default emitter.
    """


def _client_with_fake_transport(
    app_config: ServiceConfig, transport: CuratedSourceTransport
) -> TestClient:
    """Build a `TestClient` whose `GET /catalog` fetches through `transport`."""
    app = create_app(app_config)
    app.dependency_overrides[provide_curated_catalog_dependencies] = lambda: (
        build_fake_curated_catalog_dependencies(transport)
    )
    return TestClient(app)


def test_get_catalog_returns_entries_reflecting_the_fake_transports_canned_bytes(
    app_config: ServiceConfig,
) -> None:
    """AC-BI-003: the response reflects the injected transport's bytes, not any packaged file."""
    transport = FakeCuratedSourceTransport(json.dumps(_CANNED_ENTRIES).encode("utf-8"))
    client = _client_with_fake_transport(app_config, transport)

    response = client.get("/catalog")

    assert response.status_code == 200
    body = response.json()
    assert {item["instrument_id"] for item in body["instruments"]} == {"CRA-1.0", "ENGPRAC-2.1"}
    assert len(body["instruments"]) == len(_CANNED_ENTRIES)


def test_get_catalog_fetches_from_the_configured_base_url(app_config: ServiceConfig) -> None:
    """AC-BI-002 (env-var half): the transport is called against `config.curated_source_base_url`.

    No code change needed to serve a different source -- only the config
    value the injected transport is called with differs.
    """
    transport = FakeCuratedSourceTransport(json.dumps(_CANNED_ENTRIES).encode("utf-8"))
    config = dataclasses.replace(app_config, curated_source_base_url=_CUSTOM_URL)
    client = _client_with_fake_transport(config, transport)

    client.get("/catalog")

    assert len(transport.requests) == 1
    assert transport.requests[0].full_url == f"{_CUSTOM_URL}/catalog.json"


def test_get_catalog_entries_carry_the_documented_fields(app_config: ServiceConfig) -> None:
    transport = FakeCuratedSourceTransport(json.dumps(_CANNED_ENTRIES).encode("utf-8"))
    client = _client_with_fake_transport(app_config, transport)

    response = client.get("/catalog")

    body = response.json()
    for item in body["instruments"]:
        assert set(item) == {"instrument_id", "title", "source_type", "jurisdiction"}
        assert item["source_type"] in ("external", "internal")


def test_get_catalog_is_unauthenticated_never_401_or_403(app_config: ServiceConfig) -> None:
    """AC-BI-011 companion: an anonymous GET never 401/403s (mirrors GET /regulations)."""
    transport = FakeCuratedSourceTransport(json.dumps(_CANNED_ENTRIES).encode("utf-8"))
    client = _client_with_fake_transport(app_config, transport)

    response = client.get("/catalog")

    assert response.status_code not in (401, 403)


def test_get_catalog_returns_502_naming_the_source_on_malformed_response(
    app_config: ServiceConfig,
) -> None:
    """AC-BI-006: a malformed/non-JSON `catalog.json` body -> 502, no partial data."""
    transport = FakeCuratedSourceTransport(b"not json at all")
    client = _client_with_fake_transport(app_config, transport)

    response = client.get("/catalog")

    assert response.status_code == 502
    body = response.json()
    assert body["error"]["code"] == "curated_source_unavailable"
    assert "instruments" not in body


def test_get_catalog_returns_502_naming_the_source_when_unreachable(
    app_config: ServiceConfig,
) -> None:
    """AC-BI-006: an unreachable source -> 502, not a stale cached response."""
    client = _client_with_fake_transport(app_config, FakeFailingCuratedSourceTransport())

    response = client.get("/catalog")

    assert response.status_code == 502
    body = response.json()
    assert body["error"]["code"] == "curated_source_unavailable"


def test_get_catalog_succeeds_with_no_falkordb_or_llm_fixture_wired(
    app_config: ServiceConfig,
) -> None:
    """AC-BI-011: no FalkorDB/LLM dependency -- proves D-SCOPE-1/D-FAILOPEN don't regress this.

    Re-run of the existing guarantee, now backed by a fake transport rather
    than the build-time-packaged file (`lifespan` is never entered here
    either, via `create_app` + a bare `TestClient` construction, mirroring
    the shared `client` fixture's own contract).
    """
    transport = FakeCuratedSourceTransport(json.dumps(_CANNED_ENTRIES).encode("utf-8"))
    client = _client_with_fake_transport(app_config, transport)

    response = client.get("/catalog")

    assert response.status_code == 200
