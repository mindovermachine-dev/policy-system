"""Public API for `ps_service.metrics`.

Purpose: operational metrics for PS Service. Instruments are created through the
OpenTelemetry Metrics API, and the Prometheus text exposition is rendered for the
service-port `/metrics` route. The package is a leaf foundation: it imports no feature
package, so any component may record into it.

Exports: `MetricsConfigurationError`, `MetricsHandle`, `build_metrics`,
`build_metrics_router` and `record_ingestion_run`.
"""

from ps_service.metrics.errors import MetricsConfigurationError
from ps_service.metrics.provider import MetricsHandle, build_metrics
from ps_service.metrics.recorder import record_ingestion_run
from ps_service.metrics.router import build_metrics_router

__all__ = [
    "MetricsConfigurationError",
    "MetricsHandle",
    "build_metrics",
    "build_metrics_router",
    "record_ingestion_run",
]
