"""Tests for the anonymous `/metrics` route on the service port."""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi.testclient import TestClient

from metrics._app import auth_enabled_config
from ps_service.dependency_health import GRAPH_REPLAY, mark_unhealthy
from ps_service.main import create_app

if TYPE_CHECKING:
    from ps_service.metrics import MetricsHandle


def test_metrics_answers_200_text_exposition_without_credentials() -> None:
    app = create_app(auth_enabled_config())

    response = TestClient(app).get("/metrics")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain; version=0.0.4")


def test_metrics_renders_the_apps_own_handle() -> None:
    app = create_app(auth_enabled_config())
    handle: MetricsHandle = app.state.metrics
    handle.meter_provider.get_meter("ps_service").create_counter("probe.events").add(2)

    response = TestClient(app).get("/metrics")

    assert "probe_events_total" in response.text


def test_metrics_stays_200_while_the_graph_replay_gate_is_closed() -> None:
    app = create_app(auth_enabled_config())
    mark_unhealthy(GRAPH_REPLAY, error=RuntimeError("replaying"))

    gated = TestClient(app).get("/api/anything")
    metrics = TestClient(app).get("/metrics")

    assert gated.status_code == 503
    assert metrics.status_code == 200


def test_metrics_is_absent_from_the_openapi_schema() -> None:
    app = create_app(auth_enabled_config())

    assert "/metrics" not in app.openapi()["paths"]


def test_each_app_gets_its_own_metrics_handle() -> None:
    first = create_app(auth_enabled_config())
    second = create_app(auth_enabled_config())

    assert first.state.metrics is not second.state.metrics
