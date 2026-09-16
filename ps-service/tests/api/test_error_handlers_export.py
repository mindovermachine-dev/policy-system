"""Tests for the ``POST /exports`` error mapping (`ps_service.api.errors` /
`ps_service.api.error_handlers`).

Mirrors `test_error_handlers_restoration.py`'s narrower, table-driven scope:
direct calls into `register_exception_handlers`'s resulting handler map,
independent of the full route. `ExportInstrumentNotFoundError` -> 404,
`ExportConfigIncompleteError` -> 503, `ExportStageFailedError` -> 502
(naming the failing stage, the same `failing_stage` body shape
`RestoreStageFailedError`/`PipelineStageError` already use).
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from ps_service.api.error_handlers import register_exception_handlers
from ps_service.api.errors import (
    ExportConfigIncompleteError,
    ExportInstrumentNotFoundError,
    ExportStageFailedError,
)


def _build_app_that_raises(exc: Exception) -> FastAPI:
    app = FastAPI()
    register_exception_handlers(app)

    async def _boom() -> None:
        raise exc

    app.add_api_route("/boom", _boom, methods=["GET"])
    return app


def test_export_instrument_not_found_error_maps_to_404() -> None:
    client = TestClient(
        _build_app_that_raises(ExportInstrumentNotFoundError("instrument 'CRA-1.0' was not found")),
        raise_server_exceptions=False,
    )

    response = client.get("/boom")

    assert response.status_code == 404
    body = response.json()
    assert body["error"]["code"] == "export_instrument_not_found"
    assert body["error"]["message"] == "instrument 'CRA-1.0' was not found"


def test_export_config_incomplete_error_maps_to_503() -> None:
    client = TestClient(
        _build_app_that_raises(
            ExportConfigIncompleteError("PS_LLMINTERFACE_EMBED_MODEL is not set")
        ),
        raise_server_exceptions=False,
    )

    response = client.get("/boom")

    assert response.status_code == 503
    body = response.json()
    assert body["error"]["code"] == "export_config_incomplete"
    assert body["error"]["message"] == "PS_LLMINTERFACE_EMBED_MODEL is not set"


def test_export_stage_failed_error_maps_to_502_and_names_the_stage() -> None:
    exc = ExportStageFailedError(stage="serialization", reason="node with two labels")
    client = TestClient(_build_app_that_raises(exc), raise_server_exceptions=False)

    response = client.get("/boom")

    assert response.status_code == 502
    body = response.json()
    assert body["error"]["code"] == "export_stage_failed"
    assert body["error"]["failing_stage"] == "serialization"
    assert body["error"]["message"] == "node with two labels"


def test_export_errors_never_leak_a_filesystem_path_or_host_port() -> None:
    exc = ExportStageFailedError(stage="export", reason="scratch write failed at 10.0.0.5:6379")
    client = TestClient(_build_app_that_raises(exc), raise_server_exceptions=False)

    response = client.get("/boom")

    assert "10.0.0.5:6379" not in response.text
