"""Tests for the provider, resource and Prometheus exposition built by `build_metrics`."""

from __future__ import annotations

from importlib.metadata import version as installed_version
from typing import TYPE_CHECKING

from ps_service.metrics import build_metrics

if TYPE_CHECKING:
    import pytest


def test_render_returns_the_classic_prometheus_text_content_type() -> None:
    handle = build_metrics()

    _body, content_type = handle.render()

    assert content_type.startswith("text/plain; version=0.0.4")
    handle.shutdown()


def test_render_exposes_a_value_recorded_through_the_otel_api() -> None:
    handle = build_metrics()
    handle.meter_provider.get_meter("ps_service").create_counter("probe.events").add(3)

    body, _content_type = handle.render()

    assert b"probe_events_total" in body
    assert b" 3.0" in body
    handle.shutdown()


def test_two_handles_keep_independent_registries() -> None:
    first = build_metrics()
    second = build_metrics()
    first.meter_provider.get_meter("ps_service").create_counter("probe.events").add(1)

    first_body, _ = first.render()
    second_body, _ = second.render()

    assert b"probe_events_total" in first_body
    assert b"probe_events_total" not in second_body
    first.shutdown()
    second.shutdown()


def test_target_info_carries_only_service_name_and_version() -> None:
    handle = build_metrics()
    handle.meter_provider.get_meter("ps_service").create_counter("probe.events").add(1)

    body, _ = handle.render()

    target_lines = [line for line in body.decode().splitlines() if line.startswith("target_info{")]
    assert len(target_lines) == 1
    assert 'service_name="ps-service"' in target_lines[0]
    assert f'service_version="{installed_version("ps-service")}"' in target_lines[0]
    assert target_lines[0].count("=") == 2
    handle.shutdown()


def test_resource_ignores_otel_resource_attributes_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OTEL_RESOURCE_ATTRIBUTES", "host.name=leak,k8s.pod.name=leak")
    handle = build_metrics()
    handle.meter_provider.get_meter("ps_service").create_counter("probe.events").add(1)

    body, _ = handle.render()

    assert b"leak" not in body
    assert b"host_name" not in body
    handle.shutdown()
