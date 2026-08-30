"""HTTP tests for ``POST /ingestions`` -- the catalog (CELEX) ingestion path.

Increment 6 (#51a). Drives the route with ``TestClient`` and an
``app.dependency_overrides`` fake ``PipelineDependencies`` (``tests/api/_fakes.py``)
so request validation, catalog lookup, the pipeline hand-off, and error mapping
are exercised without a real graph, adapter, or LLM. The well-formed
``source: "internal"`` request is covered here too: it must validate and then
return a clean 501 referencing issue #54 (the internal pipeline is #54).

AC coverage: AC-BI-002 (runs the pipeline, reports stages), AC-BI-006 (unknown
CELEX -> 404, malformed body -> 422, both before any stage), AC-BI-008/009
(stage failure -> 502 naming the stage, message free of paths / host:port),
AC-BI-010 (run id in the body).
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient

from api._fakes import build_fake_pipeline_dependencies
from ps_service.api.dependencies import provide_pipeline_dependencies
from ps_service.config import ServiceConfig
from ps_service.domain_mapper.errors import DomainMapperExtractionError
from ps_service.main import create_app

if TYPE_CHECKING:
    from ps_service.api.ingestion_orchestration import PipelineDependencies

_VALID_CELEX = "32024R2847"
_UNKNOWN_CELEX = "32099R9999"
_HOST_PORT_RE = re.compile(r"\b[\w.\-]+:\d{2,5}\b")


def _noop_emit(**_kwargs: object) -> None:
    """Discard a run-log entry (Logging boundary stub -- see ``_stub_run_log``)."""


@pytest.fixture(autouse=True)
def _stub_run_log(  # pyright: ignore[reportUnusedFunction]  # module autouse fixture — invoked by name-collection
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stub the Logging boundary so the pipeline needs no ``configure()``d facade.

    ``run_catalog_ingestion_pipeline`` emits its ``ingestion_run`` entries through
    the process-wide default emitter. These fast HTTP tests deliberately do not
    ``configure()`` that global (a dedicated ``tests/logging`` assertion owns its
    once-only ``atexit`` registration); the run-log *content* is already covered
    against a real emitter in ``test_ingestion_orchestration.py``. Increment 7's
    ``test_run_context.py`` exercises the real facade through the HTTP layer.
    """
    monkeypatch.setattr("ps_service.api.ingestion_orchestration.emit_log_entry", _noop_emit)


def _app_config() -> ServiceConfig:
    """A loopback config with every pipeline-required value set (so the 503 guard never trips)."""
    return ServiceConfig(
        host="127.0.0.1",
        port=8000,
        graceful_shutdown_seconds=10,
        logging_dir=None,
        llm_interface_model="azure/gpt-4o",
        llm_interface_embed_model="azure/text-embedding-3-large",
        company_merge_similarity_threshold=0.83,
    )


def _client_with_fake(fake_deps: PipelineDependencies) -> TestClient:
    """A ``TestClient`` whose ``provide_pipeline_dependencies`` yields ``fake_deps``."""
    app = create_app(_app_config())
    app.dependency_overrides[provide_pipeline_dependencies] = lambda: fake_deps
    return TestClient(app, raise_server_exceptions=False)


def test_valid_celex_runs_pipeline_and_returns_run_id_and_stages() -> None:
    """AC-BI-002/010: a known CELEX runs all four stages and the body carries run id + stages."""
    fake = build_fake_pipeline_dependencies(rid="CRA-1.0")
    client = _client_with_fake(fake.dependencies)

    response = client.post("/ingestions", json={"source": "catalog", "celex": _VALID_CELEX})

    assert response.status_code == 200
    body = response.json()
    assert body["run_id"]
    assert body["regulatory_instrument_id"] == "CRA-1.0"
    assert body["source"] == "catalog"
    assert [stage["stage"] for stage in body["stages"]] == [
        "ingestion",
        "extraction",
        "derivation",
        "merge",
    ]
    assert fake.recorder.order == ["ingestion", "extraction", "derivation", "merge"]


def test_unknown_celex_returns_404_structured_and_starts_no_pipeline() -> None:
    """AC-BI-006: a CELEX absent from the curated catalog 404s before any stage runs."""
    fake = build_fake_pipeline_dependencies()
    client = _client_with_fake(fake.dependencies)

    response = client.post("/ingestions", json={"source": "catalog", "celex": _UNKNOWN_CELEX})

    assert response.status_code == 404
    body = response.json()
    assert body["error"]["code"]
    assert body["error"]["message"]
    assert "run_id" in body
    assert fake.recorder.order == []


def test_malformed_body_returns_422_structured_and_starts_no_pipeline() -> None:
    """AC-BI-006: a body that fails Pydantic validation 422s before any stage runs."""
    fake = build_fake_pipeline_dependencies()
    client = _client_with_fake(fake.dependencies)

    response = client.post("/ingestions", json={})

    assert response.status_code == 422
    body = response.json()
    assert body["error"]["code"]
    assert "run_id" in body
    assert fake.recorder.order == []


def test_stage_error_response_reports_failing_stage_and_sanitized_reason() -> None:
    """AC-BI-008/009: a stage failure 502s, names the stage, and leaks no path / host:port."""
    fake = build_fake_pipeline_dependencies(
        extract_error=DomainMapperExtractionError("no candidates for requirement unit 7"),
    )
    client = _client_with_fake(fake.dependencies)

    response = client.post("/ingestions", json={"source": "catalog", "celex": _VALID_CELEX})

    assert response.status_code == 502
    body = response.json()
    assert body["error"]["failing_stage"] == "extraction"
    message = body["error"]["message"]
    assert message
    assert "://" not in message
    assert _HOST_PORT_RE.search(message) is None
    assert fake.recorder.order == ["ingestion", "extraction"]


def test_internal_request_returns_501_referencing_54() -> None:
    """A well-formed ``source: "internal"`` request validates, then 501s naming issue #54."""
    fake = build_fake_pipeline_dependencies()
    client = _client_with_fake(fake.dependencies)

    response = client.post(
        "/ingestions",
        json={
            "source": "internal",
            "fixture_path": "engineering-practices/engineering-practices-seed.json",
        },
    )

    assert response.status_code == 501
    body = response.json()
    assert "#54" in body["error"]["message"]
    assert fake.recorder.order == []
