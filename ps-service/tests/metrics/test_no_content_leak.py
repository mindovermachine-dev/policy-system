"""The anonymous `/metrics` body carries no request, run, document or dependency content."""

from __future__ import annotations

import socket
from typing import TYPE_CHECKING

from fastapi.testclient import TestClient

from metrics._app import auth_enabled_config
from metrics._scrape import samples
from ps_service.dependency_health import (
    CELLAR_ELI,
    FALKORDB,
    GRAPH_REPLAY,
    STATE_POSTGRES,
    mark_unhealthy,
)
from ps_service.main import create_app

if TYPE_CHECKING:
    import pytest

_LEAK = "leaktoken7f3a"
_ERROR_MESSAGE = "connection refused to db-leak-host.internal:6379 password=hunter2"
_ALLOWED_LABELS = frozenset(
    {
        "http_request_method",
        "http_route",
        "http_response_status_code",
        "gating",
        "outcome",
        "le",
        "otel_scope_name",
        "otel_scope_version",
        "otel_scope_schema_url",
        "service_name",
        "service_version",
        "network_protocol_version",
    }
)
_DEPENDENCY_AND_GRAPH_NAMES = (
    "falkordb",
    "state_postgres",
    "graph_replay",
    "llm_interface",
    "cellar_eli",
    "passkey_signing_postgres",
)


def _hostile_requests(client: TestClient) -> None:
    headers = {
        "Authorization": f"Bearer {_LEAK}",
        "X-Forwarded-For": f"203.0.113.9, {_LEAK}",
        "X-Request-Id": _LEAK,
        "Host": f"{_LEAK}.example.test",
        "Cookie": f"session={_LEAK}",
    }
    client.get(f"/ingestions/{_LEAK}-run-id", headers=headers)
    client.get(f"/{_LEAK}/32024R1689?celex={_LEAK}&q={_LEAK}", headers=headers)
    client.post(
        "/ingestions",
        headers=headers,
        content=f'{{"celex_id": "{_LEAK}", "text": "{_LEAK} document text"}}',
    )
    client.request(f"X{_LEAK}", "/health", headers=headers)
    client.get(f"/mcp/{_LEAK}", headers=headers)


def _scrape_after_hostile_traffic(monkeypatch: pytest.MonkeyPatch) -> bytes:
    monkeypatch.setenv("OTEL_RESOURCE_ATTRIBUTES", f"host.name={_LEAK},k8s.pod.name={_LEAK}")
    monkeypatch.setenv("OTEL_SERVICE_NAME", _LEAK)
    monkeypatch.setenv("HOSTNAME", f"pod-{_LEAK}")
    client = TestClient(create_app(auth_enabled_config()), raise_server_exceptions=False)
    for dependency in (FALKORDB, STATE_POSTGRES, GRAPH_REPLAY, CELLAR_ELI):
        mark_unhealthy(dependency, error=RuntimeError(_ERROR_MESSAGE))
    _hostile_requests(client)
    return client.get("/metrics", headers={"X-Probe": _LEAK}).content


def test_no_request_content_reaches_the_scrape(monkeypatch: pytest.MonkeyPatch) -> None:
    body = _scrape_after_hostile_traffic(monkeypatch).decode()

    assert _LEAK not in body
    assert "32024R1689" not in body
    assert "document text" not in body
    assert "203.0.113.9" not in body


def test_no_dependency_name_graph_name_or_error_message_reaches_the_scrape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = _scrape_after_hostile_traffic(monkeypatch).decode()

    for name in _DEPENDENCY_AND_GRAPH_NAMES:
        assert name not in body
    for fragment in ("db-leak-host", "hunter2", "connection refused", "6379"):
        assert fragment not in body


def test_target_info_carries_only_the_service_name_and_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = _scrape_after_hostile_traffic(monkeypatch)

    target_info = samples(body, "target_info")
    assert len(target_info) == 1
    assert set(target_info[0].labels) == {"service_name", "service_version"}
    assert target_info[0].labels["service_name"] == "ps-service"


def test_scope_labels_carry_only_the_service_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    body = _scrape_after_hostile_traffic(monkeypatch)

    scope_names = {
        sample.labels["otel_scope_name"]
        for sample in samples(body)
        if "otel_scope_name" in sample.labels
    }
    assert scope_names == {"ps_service"}


def test_the_hostname_never_appears(monkeypatch: pytest.MonkeyPatch) -> None:
    body = _scrape_after_hostile_traffic(monkeypatch).decode()

    assert socket.gethostname() not in body


def test_label_names_stay_within_the_documented_set(monkeypatch: pytest.MonkeyPatch) -> None:
    body = _scrape_after_hostile_traffic(monkeypatch)

    seen = {label for sample in samples(body) for label in sample.labels}
    assert seen <= _ALLOWED_LABELS, sorted(seen - _ALLOWED_LABELS)
    assert {"http_route", "gating"} <= seen
