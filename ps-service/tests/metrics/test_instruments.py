"""Tests for creating instruments from catalog specs through the OpenTelemetry API."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from opentelemetry.metrics import Counter, Histogram, Observation

from ps_service.metrics import MetricsConfigurationError, build_metrics
from ps_service.metrics.catalog import METRIC_CATALOG, MetricKind, MetricSpec
from ps_service.metrics.instruments import create_instruments

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from opentelemetry.metrics import CallbackOptions

    from ps_service.metrics.instruments import Instrument


def _histogram_named(instruments: Mapping[str, Instrument], name: str) -> Histogram:
    instrument = instruments[name]
    assert isinstance(instrument, Histogram)
    return instrument


def _counter_named(instruments: Mapping[str, Instrument], name: str) -> Counter:
    instrument = instruments[name]
    assert isinstance(instrument, Counter)
    return instrument


def _histogram() -> MetricSpec:
    return MetricSpec(
        name="probe.duration",
        kind=MetricKind.HISTOGRAM,
        unit="s",
        labels=("probe.kind",),
        description="Probe duration.",
        bucket_boundaries=(1.0, 5.0),
    )


def _counter() -> MetricSpec:
    return MetricSpec(
        name="probe.events",
        kind=MetricKind.COUNTER,
        unit="{event}",
        labels=("outcome",),
        description="Probe events.",
    )


def _gauge() -> MetricSpec:
    return MetricSpec(
        name="probe.level",
        kind=MetricKind.OBSERVABLE_GAUGE,
        unit="{item}",
        labels=(),
        description="Probe level.",
    )


def _level_observer(_options: CallbackOptions) -> Iterable[Observation]:
    return [Observation(7)]


def test_catalog_entries_have_unique_otel_names() -> None:
    names = [spec.name for spec in METRIC_CATALOG]

    assert len(names) == len(set(names))


def test_each_kind_is_created_and_exposed_after_a_value_is_recorded() -> None:
    handle = build_metrics()
    instruments = create_instruments(
        handle.meter_provider.get_meter("ps_service"),
        (_histogram(), _counter(), _gauge()),
        {"probe.level": _level_observer},
    )
    _histogram_named(instruments, "probe.duration").record(2.0, {"probe.kind": "a"})
    _counter_named(instruments, "probe.events").add(1, {"outcome": "ok"})

    body = handle.render()[0].decode()

    assert 'probe_duration_seconds_bucket{le="5.0"' in body
    assert "probe_events_total{" in body
    assert "probe_level" in body
    handle.shutdown()


def test_histogram_uses_the_spec_bucket_boundaries() -> None:
    handle = build_metrics()
    instruments = create_instruments(
        handle.meter_provider.get_meter("ps_service"), (_histogram(),), {}
    )
    _histogram_named(instruments, "probe.duration").record(0.5, {"probe.kind": "a"})

    body = handle.render()[0].decode()

    assert 'le="1.0"' in body
    assert 'le="5.0"' in body
    assert 'le="0.005"' not in body
    handle.shutdown()


def test_gauge_without_an_observer_is_rejected() -> None:
    handle = build_metrics()

    with pytest.raises(MetricsConfigurationError):
        create_instruments(handle.meter_provider.get_meter("ps_service"), (_gauge(),), {})
    handle.shutdown()


def test_observer_for_an_unknown_metric_is_rejected() -> None:
    handle = build_metrics()

    with pytest.raises(MetricsConfigurationError):
        create_instruments(
            handle.meter_provider.get_meter("ps_service"), (), {"typo.metric": _level_observer}
        )
    handle.shutdown()
