"""The metric catalog: the single list of every metric `ps-service` registers.

Purpose: one source for instrument creation and for the operations-guide drift test, so a
metric cannot be registered without being documented. Each entry is added by the change
that introduces its instrument and recording path.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class MetricKind(StrEnum):
    """The instrument types the catalog can describe."""

    HISTOGRAM = "histogram"
    COUNTER = "counter"
    OBSERVABLE_GAUGE = "observable_gauge"


@dataclass(frozen=True)
class MetricSpec:
    """The definition of one metric.

    Attributes:
        name: OpenTelemetry metric name, for example `http.server.request.duration`.
        kind: The instrument type to create.
        unit: UCUM unit, for example `s` or `{run}`.
        labels: Attribute names the metric carries; never user or document content.
        description: One-sentence meaning, used as the instrument description.
        bucket_boundaries: Explicit histogram bucket upper bounds in `unit`; `None` for other
            kinds and for the SDK defaults.
    """

    name: str
    kind: MetricKind
    unit: str
    labels: tuple[str, ...]
    description: str
    bucket_boundaries: tuple[float, ...] | None = None


REQUEST_DURATION = "http.server.request.duration"
DEPENDENCIES_UNHEALTHY = "ps_service.dependencies.unhealthy"
INGESTION_RUNS = "ps_service.ingestion.runs"
INGESTION_RUN_DURATION = "ps_service.ingestion.run.duration"
GRAPH_LOG_PENDING = "ps_service.graph_log.pending_entries"

METRIC_CATALOG: tuple[MetricSpec, ...] = (
    MetricSpec(
        name=REQUEST_DURATION,
        kind=MetricKind.HISTOGRAM,
        unit="s",
        labels=("http.request.method", "http.route", "http.response.status_code"),
        description="Duration of HTTP server requests, by method, route template and status.",
        bucket_boundaries=(
            0.005,
            0.01,
            0.025,
            0.05,
            0.075,
            0.1,
            0.25,
            0.5,
            0.75,
            1.0,
            2.5,
            5.0,
            7.5,
            10.0,
        ),
    ),
    MetricSpec(
        name=DEPENDENCIES_UNHEALTHY,
        kind=MetricKind.OBSERVABLE_GAUGE,
        unit="{dependency}",
        labels=("gating",),
        description="Unhealthy external dependencies, split by whether they gate readiness.",
    ),
    MetricSpec(
        name=INGESTION_RUNS,
        kind=MetricKind.COUNTER,
        unit="{run}",
        labels=("outcome",),
        description="Ingestion runs that reached a terminal state, by outcome.",
    ),
    MetricSpec(
        name=INGESTION_RUN_DURATION,
        kind=MetricKind.HISTOGRAM,
        unit="s",
        labels=("outcome",),
        description="Duration of ingestion runs from submission to completion, by outcome.",
        bucket_boundaries=(1.0, 5.0, 15.0, 30.0, 60.0, 120.0, 300.0, 600.0, 1200.0, 1800.0, 3600.0),
    ),
    MetricSpec(
        name=GRAPH_LOG_PENDING,
        kind=MetricKind.OBSERVABLE_GAUGE,
        unit="{entry}",
        labels=(),
        description="Graph mutation log entries written but not yet applied, across all graphs.",
    ),
)
