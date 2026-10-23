"""HTTP tests for ``GET /ingestions/{run_id}`` -- the live in-flight-stage read.

A best-effort progress read over
``ps_service.api.run_status``: always 200, ``stage: null`` for an unknown,
completed, or not-yet-started run id -- never a 404 (this is a
best-effort live-progress read, not authoritative resource retrieval).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast
from urllib.parse import unquote

import pytest

from ps_service.api.models import IDENTIFIER_PATTERN
from ps_service.api.run_status import clear_stage, set_stage
from ps_service.logging import bind_run_context

if TYPE_CHECKING:
    from fastapi.testclient import TestClient


def test_get_ingestion_status_returns_current_stage_for_a_tracked_run_id(
    client: TestClient,
) -> None:
    """A run_id with a recorded stage in the registry reports that stage."""
    run_id = "status-run-tracked"
    set_stage(run_id, "extraction")
    try:
        response = client.get(f"/ingestions/{run_id}")
    finally:
        clear_stage(run_id)

    assert response.status_code == 200
    body = response.json()
    assert body == {"run_id": run_id, "stage": "extraction"}


def test_get_ingestion_status_returns_null_stage_for_unknown_run_id(
    client: TestClient,
) -> None:
    """An unknown/completed/not-yet-started run_id still 200s,
    with ``stage: null`` -- never a 404.
    """
    response = client.get("/ingestions/status-run-never-seen")

    assert response.status_code == 200
    assert response.json() == {"run_id": "status-run-never-seen", "stage": None}


_MALFORMED_RUN_IDS = [
    pytest.param("r" * 65, id="65-characters"),
    pytest.param("run.id", id="dot"),
    pytest.param("run%20id", id="space"),
    pytest.param("run;id", id="semicolon"),
    pytest.param("run-id%0A", id="trailing-newline"),
    pytest.param("run%C3%A9", id="non-ascii-letter"),
]


def _generic_validation_body(client: TestClient) -> dict[str, object]:
    """The body this app returns for any request that fails validation."""
    response = client.post("/near-misses/review_aaa/resolve", json={})
    assert response.status_code == 422
    return response.json()


@pytest.mark.parametrize("run_id", _MALFORMED_RUN_IDS)
def test_get_ingestion_status_rejects_malformed_run_id_with_the_generic_422(
    client: TestClient, run_id: str
) -> None:
    response = client.get(f"/ingestions/{run_id}")

    assert response.status_code == 422
    assert response.json() == _generic_validation_body(client)
    assert "stage" not in response.json()
    assert unquote(run_id) not in response.text


def test_get_ingestion_status_accepts_every_system_generated_run_id_shape(
    client: TestClient,
) -> None:
    with bind_run_context() as generated:
        run_ids = [generated, "r" * 64, "r", "run_id-with_both-1"]
    for run_id in run_ids:
        response = client.get(f"/ingestions/{run_id}")

        assert response.status_code == 200
        assert response.json() == {"run_id": run_id, "stage": None}


def test_openapi_publishes_the_run_id_pattern(client: TestClient) -> None:
    document = cast("dict[str, Any]", client.get("/openapi.json").json())
    parameters = cast(
        "list[dict[str, Any]]", document["paths"]["/ingestions/{run_id}"]["get"]["parameters"]
    )
    (parameter,) = [p for p in parameters if p["name"] == "run_id"]

    assert parameter["in"] == "path"
    assert parameter["schema"]["pattern"] == IDENTIFIER_PATTERN
