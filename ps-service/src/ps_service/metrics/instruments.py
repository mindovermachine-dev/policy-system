"""Create the catalog's instruments through the OpenTelemetry Metrics API only."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ps_service.metrics.catalog import MetricKind
from ps_service.metrics.errors import MetricsConfigurationError

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping, Sequence

    from opentelemetry.metrics import CallbackOptions, Counter, Histogram, Meter, Observation
    from opentelemetry.metrics import ObservableGauge as ObservableGaugeInstrument

    from ps_service.metrics.catalog import MetricSpec

    type Observer = Callable[[CallbackOptions], Iterable[Observation]]
    type Instrument = Histogram | Counter | ObservableGaugeInstrument


def create_instruments(
    meter: Meter, specs: Sequence[MetricSpec], observers: Mapping[str, Observer]
) -> dict[str, Instrument]:
    """Create one instrument per spec on `meter`.

    Args:
        meter: The meter that owns the instruments.
        specs: The metric definitions to create, normally `METRIC_CATALOG`.
        observers: Callback per observable-gauge metric name.

    Returns:
        The instruments keyed by OpenTelemetry metric name.

    Raises:
        MetricsConfigurationError: If an observable gauge has no observer, or an observer
            names a metric that is not an observable gauge in `specs`.

    Side Effects:
        Registers the instruments on the meter's provider.
    """
    gauge_names = {spec.name for spec in specs if spec.kind is MetricKind.OBSERVABLE_GAUGE}
    unknown = sorted(set(observers) - gauge_names)
    if unknown:
        raise MetricsConfigurationError(f"observers for unknown gauges: {unknown}")
    return {spec.name: _create_one(meter, spec, observers) for spec in specs}


def _create_one(meter: Meter, spec: MetricSpec, observers: Mapping[str, Observer]) -> Instrument:
    """Create the instrument a single spec describes."""
    if spec.kind is MetricKind.HISTOGRAM:
        return meter.create_histogram(
            spec.name,
            unit=spec.unit,
            description=spec.description,
            explicit_bucket_boundaries_advisory=spec.bucket_boundaries,
        )
    if spec.kind is MetricKind.COUNTER:
        return meter.create_counter(spec.name, unit=spec.unit, description=spec.description)
    observer = observers.get(spec.name)
    if observer is None:
        raise MetricsConfigurationError(f"observable gauge {spec.name} has no observer")
    return meter.create_observable_gauge(
        spec.name, callbacks=[observer], unit=spec.unit, description=spec.description
    )
