"""Build the OpenTelemetry meter provider and the handle the app keeps on `app.state`."""

from __future__ import annotations

from dataclasses import dataclass
from importlib.metadata import version as installed_version
from typing import TYPE_CHECKING

from opentelemetry.metrics import Counter, Histogram
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.resources import Resource

from ps_service.metrics.catalog import (
    DEPENDENCIES_UNHEALTHY,
    GRAPH_LOG_PENDING,
    INGESTION_RUN_DURATION,
    INGESTION_RUNS,
    METRIC_CATALOG,
)
from ps_service.metrics.errors import MetricsConfigurationError
from ps_service.metrics.exposition import PrometheusExposition
from ps_service.metrics.instruments import create_instruments
from ps_service.metrics.observers import dependency_observations, guarded, pending_observations
from ps_service.metrics.recorder import IngestionRecorder, install, uninstall

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from opentelemetry.metrics import Observation

    from ps_service.metrics.instruments import Instrument

_SERVICE_NAME = "ps-service"
_METER_NAME = "ps_service"


@dataclass(frozen=True)
class MetricsHandle:
    """The per-app metrics state: a meter provider and its Prometheus exposition.

    Attributes:
        meter_provider: Provider that owns every instrument of one app.
        instruments: The catalog's instruments keyed by OpenTelemetry metric name.
    """

    meter_provider: MeterProvider
    instruments: Mapping[str, Instrument]
    _exposition: PrometheusExposition
    _recorder: IngestionRecorder

    def histogram(self, name: str) -> Histogram:
        """Return the histogram instrument registered under `name`.

        Args:
            name: OpenTelemetry metric name of a histogram in the catalog.

        Returns:
            The histogram instrument.

        Raises:
            MetricsConfigurationError: If `name` is not a registered histogram.
        """
        return _histogram(self.instruments, name)

    def render(self) -> tuple[bytes, str]:
        """Render the current metrics as a Prometheus scrape body.

        Returns:
            The exposition body and its `Content-Type` header value.

        Side Effects:
            Collects every instrument, running observable callbacks.
        """
        return self._exposition.render()

    def shutdown(self) -> None:
        """Shut the meter provider down; call once, when the app stops.

        Side Effects:
            Stops the provider, whose reader unregisters its collector from the private registry,
            and stops ingestion recording into this handle if it is still the installed one.
        """
        uninstall(self._recorder)
        self.meter_provider.shutdown()


def build_metrics(
    *,
    unhealthy_counts: Callable[[], tuple[int, int]] | None = None,
    pending_entries: Callable[[], int | None] | None = None,
) -> MetricsHandle:
    """Build an isolated meter provider whose reader publishes to a private registry.

    The resource is built with the `Resource` constructor, not `Resource.create`, so
    `OTEL_RESOURCE_ATTRIBUTES` and the host never reach `target_info`: it carries only the
    service name and version.

    Args:
        unhealthy_counts: Returns `(gating_unhealthy, non_gating_unhealthy)` dependency counts,
            read from an existing registry with no probe. `None` leaves the gauge without
            series. A source that raises is logged and omitted from that scrape.
        pending_entries: Returns the total of pending graph-log entries, or `None` while there is
            no value. It must be bounded and cached by the caller (`TtlCachedSource`), since a
            scrape calls it. `None` leaves the gauge without a series; a source that raises is
            logged and omitted from that scrape.

    Returns:
        A new `MetricsHandle`.

    Raises:
        PackageNotFoundError: If the `ps-service` distribution is not installed.

    Side Effects:
        Creates a provider, a registry, a reader and the catalog's instruments, and installs the
        ingestion run instruments as the process's recorder (last writer wins); touches no global
        OpenTelemetry state.
    """
    exposition = PrometheusExposition()
    resource = Resource(
        {"service.name": _SERVICE_NAME, "service.version": installed_version(_SERVICE_NAME)}
    )
    provider = MeterProvider(resource=resource, metric_readers=[exposition.reader])
    observers = {
        DEPENDENCIES_UNHEALTHY: guarded(
            "dependencies_unhealthy",
            _no_series if unhealthy_counts is None else dependency_observations(unhealthy_counts),
        ),
        GRAPH_LOG_PENDING: guarded(
            "graph_log_pending_entries",
            _no_series if pending_entries is None else pending_observations(pending_entries),
        ),
    }
    instruments = create_instruments(provider.get_meter(_METER_NAME), METRIC_CATALOG, observers)
    ingestion = IngestionRecorder(
        runs=_counter(instruments, INGESTION_RUNS),
        duration=_histogram(instruments, INGESTION_RUN_DURATION),
    )
    install(ingestion)
    return MetricsHandle(
        meter_provider=provider,
        instruments=instruments,
        _exposition=exposition,
        _recorder=ingestion,
    )


def _no_series() -> list[Observation]:
    """The producer of a gauge whose source is not wired: it reports nothing."""
    return []


def _histogram(instruments: Mapping[str, Instrument], name: str) -> Histogram:
    """Return the histogram registered under `name`, or raise `MetricsConfigurationError`."""
    instrument = instruments.get(name)
    if not isinstance(instrument, Histogram):
        raise MetricsConfigurationError(f"{name} is not a registered histogram")
    return instrument


def _counter(instruments: Mapping[str, Instrument], name: str) -> Counter:
    """Return the counter registered under `name`, or raise `MetricsConfigurationError`."""
    instrument = instruments.get(name)
    if not isinstance(instrument, Counter):
        raise MetricsConfigurationError(f"{name} is not a registered counter")
    return instrument
