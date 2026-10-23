"""The pending graph-log gauge as wired by `create_app`, scraped through `/metrics`."""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi.testclient import TestClient

from metrics._app import auth_enabled_config
from metrics._logs import metrics_entries
from metrics._scrape import samples
from ps_service.main import create_app

if TYPE_CHECKING:
    from pathlib import Path

_GAUGE = "ps_service_graph_log_pending_entries"


def test_scrape_reports_the_injected_total() -> None:
    client = TestClient(create_app(auth_enabled_config(), pending_entries=lambda: 11))

    response = client.get("/metrics")

    assert [s.value for s in samples(response.content, _GAUGE)] == [11.0]


def test_a_failing_source_leaves_the_scrape_200_with_the_other_families(
    configured_logging: Path,
) -> None:
    def failing() -> int:
        raise ConnectionError("pg.internal:5432 refused")

    client = TestClient(create_app(auth_enabled_config(), pending_entries=failing))
    client.get("/health")

    response = client.get("/metrics")

    assert response.status_code == 200
    assert _GAUGE not in response.text
    assert "pg.internal" not in response.text
    assert "http_server_request_duration_seconds" in response.text
    assert "ps_service_dependencies_unhealthy" in response.text
    [entry] = metrics_entries(configured_logging, "collect_graph_log_pending_entries")
    assert entry["error_type"] == "ConnectionError"


def test_the_default_source_without_a_configured_postgres_omits_the_series() -> None:
    client = TestClient(create_app(auth_enabled_config()))

    response = client.get("/metrics")

    assert response.status_code == 200
    assert _GAUGE not in response.text
