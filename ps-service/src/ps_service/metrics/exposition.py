"""Prometheus text exposition for the OpenTelemetry metric reader.

Purpose: the single boundary module that imports `prometheus_client`. The OpenTelemetry
Prometheus reader publishes into a `CollectorRegistry` and has no text renderer of its own,
so rendering the scrape body on the service port needs `generate_latest`. No instrument is
created here; instruments come only from the OpenTelemetry Metrics API. Each
`PrometheusExposition` owns a private registry, so several apps built in one process never
stack collectors on the global default registry.
"""

from __future__ import annotations

from opentelemetry.exporter.prometheus import PrometheusMetricReader
from prometheus_client import CollectorRegistry
from prometheus_client.exposition import CONTENT_TYPE_PLAIN_0_0_4, generate_latest


class PrometheusExposition:
    """A private Prometheus registry fed by one OpenTelemetry metric reader.

    Business Rules:
        The registry is private to the instance, and the reader keeps its default resource and
        scope labels; the caller decides what the resource carries.
    """

    def __init__(self) -> None:
        """Create the private registry and the reader that publishes into it.

        Side Effects:
            Registers the reader's collector on the new registry only.
        """
        self._registry = CollectorRegistry()
        self.reader = PrometheusMetricReader(registry=self._registry)

    def render(self) -> tuple[bytes, str]:
        """Collect every instrument and serialise it in the Prometheus text format.

        Returns:
            The exposition body and its `Content-Type` header value.

        Side Effects:
            Triggers a collection of the reader, which runs every observable callback.
        """
        return generate_latest(self._registry), CONTENT_TYPE_PLAIN_0_0_4
