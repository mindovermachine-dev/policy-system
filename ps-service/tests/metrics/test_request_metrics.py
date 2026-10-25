"""Request metrics through a real `create_app`: bounded labels and the middleware order."""

from __future__ import annotations

from fastapi.testclient import TestClient

from metrics._app import auth_enabled_config
from metrics._scrape import label_values, samples
from ps_service.dependency_health import GRAPH_REPLAY, mark_unhealthy
from ps_service.main import (
    _ReplayReadGateMiddleware,  # pyright: ignore[reportPrivateUsage]  # asserts the middleware order against the gate class
    create_app,
)
from ps_service.metrics.http_middleware import MetricsMiddleware

_COUNT = "http_server_request_duration_seconds_count"
_ROUTE = "http_route"
_METHOD = "http_request_method"
_STATUS = "http_response_status_code"
_UNMATCHED_COUNT = 1000


def _client() -> TestClient:
    return TestClient(create_app(auth_enabled_config()), raise_server_exceptions=False)


def _scrape(client: TestClient) -> bytes:
    return client.get("/metrics").content


def _series_by_route(client: TestClient) -> dict[tuple[str, str, str], float]:
    return {
        (s.labels[_METHOD], s.labels[_ROUTE], s.labels[_STATUS]): s.value
        for s in samples(_scrape(client), _COUNT)
    }


def test_metrics_middleware_is_outermost_ahead_of_the_replay_gate() -> None:
    app = create_app(auth_enabled_config())

    assert app.user_middleware[0].cls is MetricsMiddleware
    assert app.user_middleware[1].cls is _ReplayReadGateMiddleware


def test_a_real_route_is_recorded_under_its_template() -> None:
    client = _client()

    client.get("/livez")

    assert _series_by_route(client)[("GET", "/livez", "200")] == 1.0


def test_thousand_distinct_unmatched_paths_share_one_route_value() -> None:
    client = _client()
    client.get("/nothing/0")
    _scrape(client)
    series_before = len(_series_by_route(client))

    for index in range(1, _UNMATCHED_COUNT + 1):
        client.get(f"/nothing/{index}/{index}")

    body = _scrape(client)
    assert len(_series_by_route(client)) == series_before
    assert "nothing" not in body.decode()
    unmatched = [s for s in samples(body, _COUNT) if s.labels[_ROUTE] == "__unmatched__"]
    assert [s.value for s in unmatched] == [float(_UNMATCHED_COUNT + 1)]


def test_thousand_distinct_nonstandard_methods_share_the_other_method_value() -> None:
    client = _client()
    client.request("FOO0000", "/livez")
    _scrape(client)
    series_before = len(_series_by_route(client))

    for index in range(1, _UNMATCHED_COUNT + 1):
        client.request(f"FOO{index:04d}", "/livez")

    body = _scrape(client)
    assert len(_series_by_route(client)) == series_before
    assert "FOO" not in body.decode()
    assert "_OTHER" in label_values(body, _COUNT, _METHOD)


def test_replay_gate_503_is_counted_with_the_route_template_or_the_unmatched_value() -> None:
    client = _client()
    mark_unhealthy(GRAPH_REPLAY, error=RuntimeError("replaying"))

    gated = client.get("/ingestions/some-run-id")
    unknown = client.get("/no/such/path")

    series = _series_by_route(client)
    assert (gated.status_code, unknown.status_code) == (503, 503)
    assert series[("GET", "/ingestions/{run_id}", "503")] == 1.0
    assert series[("GET", "__unmatched__", "503")] == 1.0
    assert "some-run-id" not in _scrape(client).decode()


def test_authentication_401_is_counted_with_the_route_template() -> None:
    client = _client()

    rejected = client.get("/ingestions/some-run-id")

    assert rejected.status_code == 401
    assert _series_by_route(client)[("GET", "/ingestions/{run_id}", "401")] == 1.0


def test_mcp_sub_paths_all_land_on_the_mount_prefix() -> None:
    client = _client()

    for suffix in ("a1b2", "c3/d4", "e5/f6/g7"):
        client.get(f"/mcp/{suffix}")

    routes = label_values(_scrape(client), _COUNT, _ROUTE)
    assert "/mcp" in routes
    assert not any(route.startswith("/mcp/") for route in routes)
