"""Tests for the ``POST /restorations`` error mapping (`ps_service.api.errors` /
`ps_service.api.error_handlers`).

Mirrors ``test_error_handlers.py``'s table-driven style:
``RestoreArtifactRejectedError`` -> 422, ``RestoreStageFailedError`` -> 502
(naming the failing stage, the same ``failing_stage`` body shape
``PipelineStageError`` already uses).
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from ps_service.api.error_handlers import register_exception_handlers
from ps_service.api.errors import RestoreArtifactRejectedError, RestoreStageFailedError


def _build_app_that_raises(exc: Exception) -> FastAPI:
    app = FastAPI()
    register_exception_handlers(app)

    async def _boom() -> None:
        raise exc

    app.add_api_route("/boom", _boom, methods=["GET"])
    return app


def test_restore_artifact_rejected_error_maps_to_422() -> None:
    client = TestClient(
        _build_app_that_raises(RestoreArtifactRejectedError("checksum mismatch")),
        raise_server_exceptions=False,
    )

    response = client.get("/boom")

    assert response.status_code == 422
    body = response.json()
    assert body["error"]["code"] == "restore_artifact_rejected"
    assert body["error"]["message"] == "checksum mismatch"


def test_restore_stage_failed_error_maps_to_502_and_names_the_stage() -> None:
    exc = RestoreStageFailedError(stage="staging", reason="native blob rejected")
    client = TestClient(_build_app_that_raises(exc), raise_server_exceptions=False)

    response = client.get("/boom")

    assert response.status_code == 502
    body = response.json()
    assert body["error"]["code"] == "restore_stage_failed"
    assert body["error"]["failing_stage"] == "staging"
    assert body["error"]["message"] == "native blob rejected"


def test_restoration_errors_never_leak_a_filesystem_path_or_host_port() -> None:
    exc = RestoreStageFailedError(
        stage="merge", reason="FalkorDB connection failed at 10.0.0.5:6379"
    )
    client = TestClient(_build_app_that_raises(exc), raise_server_exceptions=False)

    response = client.get("/boom")

    assert "10.0.0.5:6379" not in response.text
