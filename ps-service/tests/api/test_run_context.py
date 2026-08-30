"""End-to-end run-id propagation tests for the PS Service REST API (Increment 7, #51a).

Proves the async ``provide_run_id`` generator dependency's ``bind_run_context()``
binding actually reaches:

* the log lines the ingestion orchestration emits *during* the request, through
  the real process-default Logging emitter (not a stub), and
* the ``run_id`` field of a structured error body (read from
  ``request.state.run_id`` by the exception handler).

A fully-stubbed Logging boundary would not prove the first point, so
``test_log_lines_emitted_during_a_request_carry_the_returned_run_id`` runs
against a real ``configure()``d facade (the ``configured_logging`` fixture).

AC coverage: AC-BI-010 (run context bound; every log line carries the run id;
the run id is returned to the caller).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient

from api._fakes import build_fake_pipeline_dependencies
from ps_service.api.dependencies import provide_pipeline_dependencies
from ps_service.config import ServiceConfig
from ps_service.domain_mapper.errors import DomainMapperExtractionError
from ps_service.logging import facade
from ps_service.main import create_app

if TYPE_CHECKING:
    from pathlib import Path

    from api._fakes import ReadLines
    from ps_service.api.ingestion_orchestration import PipelineDependencies

_VALID_CELEX = "32024R2847"


def _app_config() -> ServiceConfig:
    """A loopback config with every pipeline-required value set (the 503 guard never trips)."""
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


def test_log_lines_emitted_during_a_request_carry_the_returned_run_id(
    configured_logging: Path, read_lines: ReadLines
) -> None:
    """AC-BI-010: every line the orchestration emits carries the run id returned to the caller.

    The route binds one ``bind_run_context()`` in the async ``provide_run_id``
    dependency and hands the pipeline no explicit emitter, so the
    ``ingestion_run`` entries flow through the process-default emitter that
    ``configured_logging`` installed. Their ``run_id`` must equal the ``run_id``
    in the response body -- proof the binding propagated from the request task
    into the ``run_in_threadpool`` pipeline work.
    """
    fake = build_fake_pipeline_dependencies(rid="CRA-1.0")
    client = _client_with_fake(fake.dependencies)

    response = client.post("/ingestions", json={"source": "catalog", "celex": _VALID_CELEX})

    assert response.status_code == 200
    returned_run_id = response.json()["run_id"]
    assert returned_run_id

    facade.reset_for_tests()  # drain + join the writer thread so the file is complete
    lines = read_lines(configured_logging)

    assert lines, "the orchestration emitted no lines through the process-default emitter"
    assert all(line.get("run_id") == returned_run_id for line in lines)
    assert any(line.get("action") == "ingestion_run" for line in lines)


@pytest.mark.usefixtures("configured_logging")
def test_error_body_carries_the_request_run_id() -> None:
    """AC-BI-010: a structured error body carries the run id bound for the request.

    A failing stage yields a 502 ``PipelineStageError`` body; its ``run_id`` is
    read from ``request.state.run_id``, which ``provide_run_id`` set from the same
    ``bind_run_context()`` binding.
    """
    fake = build_fake_pipeline_dependencies(
        extract_error=DomainMapperExtractionError("no candidates for requirement unit 7"),
    )
    client = _client_with_fake(fake.dependencies)

    response = client.post("/ingestions", json={"source": "catalog", "celex": _VALID_CELEX})

    assert response.status_code == 502
    run_id = response.json()["run_id"]
    assert isinstance(run_id, str)
    assert run_id
    assert fake.recorder.order == ["ingestion", "extraction"]


def test_get_regulations_binds_a_fresh_run_id_per_request(client: TestClient) -> None:
    """AC-BI-010: each request gets its own ``bind_run_context()`` binding, returned in the body."""
    first = client.get("/regulations")
    second = client.get("/regulations")

    assert first.status_code == 200
    assert second.status_code == 200
    first_run_id = first.json()["run_id"]
    second_run_id = second.json()["run_id"]
    assert first_run_id
    assert second_run_id
    assert first_run_id != second_run_id
