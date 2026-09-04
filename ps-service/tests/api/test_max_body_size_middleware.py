"""Tests for `ServiceConfig.max_request_body_bytes` + `main._MaxBodySizeMiddleware`
(CHANGES.md OQ7, new Slice 6.9).

Placed under `tests/api/` (this task's declared scope) even though
`ServiceConfig` itself lives in `ps_service/config.py` -- the field and the
middleware are one OQ7 unit, and the middleware is what these tests actually
exercise end to end via `TestClient`.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from ps_service.config import ServiceConfig, ServiceConfigurationError, load_config
from ps_service.main import create_app


def test_service_config_defaults_max_request_body_bytes_to_100_mebibytes() -> None:
    """CHANGES.md OQ7: default is 104_857_600 (100 MiB)."""
    config = ServiceConfig(
        host="127.0.0.1", port=8000, graceful_shutdown_seconds=10, logging_dir=None
    )

    assert config.max_request_body_bytes == 104_857_600


def test_load_config_reads_max_request_body_bytes_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PS_SERVICE_MAX_REQUEST_BODY_BYTES", "1024")

    config = load_config()

    assert config.max_request_body_bytes == 1024


def test_load_config_rejects_non_integer_max_request_body_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PS_SERVICE_MAX_REQUEST_BODY_BYTES", "not-a-number")

    with pytest.raises(ServiceConfigurationError):
        load_config()


def test_load_config_rejects_non_positive_max_request_body_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PS_SERVICE_MAX_REQUEST_BODY_BYTES", "0")

    with pytest.raises(ServiceConfigurationError):
        load_config()


def _app_config(max_bytes: int) -> ServiceConfig:
    return ServiceConfig(
        host="127.0.0.1",
        port=8000,
        graceful_shutdown_seconds=10,
        logging_dir=None,
        max_request_body_bytes=max_bytes,
    )


def test_request_under_the_limit_is_not_rejected() -> None:
    client = TestClient(create_app(_app_config(max_bytes=1_000_000)))

    response = client.post("/ingestions", json={"source": "catalog", "celex": "32024R2847"})

    # Whatever the route itself does (200/404/502/...), it must not be the
    # body-size middleware's own 413 -- the point of this test is that a
    # small body passes the middleware and reaches routing.
    assert response.status_code != 413


def test_request_over_the_limit_returns_413_with_structured_body() -> None:
    client = TestClient(create_app(_app_config(max_bytes=10)))

    response = client.post(
        "/ingestions", json={"source": "catalog", "celex": "32024R2847", "padding": "x" * 100}
    )

    assert response.status_code == 413
    body = response.json()
    assert body["error"]["code"] == "request_body_too_large"
    assert body["error"]["message"]
    assert "run_id" in body


def test_request_over_the_limit_is_rejected_for_any_route_not_only_ingestions() -> None:
    """The middleware inspects every HTTP request generically -- not one route's own body."""
    client = TestClient(create_app(_app_config(max_bytes=10)))

    response = client.post("/restorations", json={"instrument_id": "x" * 200, "padding": "y" * 200})

    assert response.status_code == 413
