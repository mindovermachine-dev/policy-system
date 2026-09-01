"""HTTP tests for ``GET /ingestions/{run_id}`` -- the live in-flight-stage read.

Increment 12 (#61, AC-BI-008/011). A best-effort progress read over
``ps_service.api.run_status``: always 200, ``stage: null`` for an unknown,
completed, or not-yet-started run id -- never a 404 (D4: this is a
best-effort live-progress read, not authoritative resource retrieval).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ps_service.api.run_status import clear_stage, set_stage

if TYPE_CHECKING:
    from fastapi.testclient import TestClient


def test_get_ingestion_status_returns_current_stage_for_a_tracked_run_id(
    client: TestClient,
) -> None:
    """AC-BI-008: a run_id with a recorded stage in the registry reports it verbatim."""
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
    """AC-BI-011 (via D4): an unknown/completed/not-yet-started run_id still 200s,
    with ``stage: null`` -- never a 404.
    """
    response = client.get("/ingestions/status-run-never-seen")

    assert response.status_code == 200
    assert response.json() == {"run_id": "status-run-never-seen", "stage": None}
