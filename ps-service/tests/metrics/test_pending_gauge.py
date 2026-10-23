"""The pending graph-log gauge: one unlabelled total, fed by an injected source."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from metrics._catalog_checks import scrape_family_names
from metrics._logs import metrics_entries
from metrics._scrape import samples
from ps_service.metrics import build_metrics
from ps_service.metrics.sources import TtlCachedSource

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

_GAUGE = "ps_service_graph_log_pending_entries"


def _cached(fetch: Callable[[], int]) -> TtlCachedSource[int]:
    return TtlCachedSource(
        "graph_log_pending_entries", fetch, ttl_seconds=15.0, timeout_seconds=1.0
    )


def test_the_gauge_has_no_labels_and_equals_the_injected_total() -> None:
    handle = build_metrics(pending_entries=_cached(lambda: 42).get)

    [sample] = samples(handle.render()[0], _GAUGE)

    assert sample.value == 42.0
    assert set(sample.labels) <= {
        "otel_scope_name",
        "otel_scope_version",
        "otel_scope_schema_url",
    }
    handle.shutdown()


def test_a_zero_total_is_a_series_not_an_omission() -> None:
    handle = build_metrics(pending_entries=_cached(lambda: 0).get)

    assert [s.value for s in samples(handle.render()[0], _GAUGE)] == [0.0]
    handle.shutdown()


def test_a_failing_source_omits_the_family_and_leaks_nothing(configured_logging: Path) -> None:
    def failing() -> int:
        raise ConnectionError("pg-host.internal:5432 SELECT max(position) FROM graph_log.entries")

    handle = build_metrics(unhealthy_counts=lambda: (0, 0), pending_entries=_cached(failing).get)

    first = handle.render()[0]
    second = handle.render()[0]

    assert _GAUGE not in first.decode()
    assert "ps_service_dependencies_unhealthy" in scrape_family_names(first)
    for body in (first, second):
        text = body.decode()
        assert "pg-host" not in text
        assert "graph_log" not in text.replace("ps_service_graph_log", "")
        assert "SELECT" not in text
    [entry] = metrics_entries(configured_logging, "collect_graph_log_pending_entries")
    assert entry["error_type"] == "ConnectionError"
    assert "pg-host" not in json.dumps(entry)
    handle.shutdown()


def test_many_scrapes_call_the_underlying_source_once_per_ttl() -> None:
    calls = 0

    def counted() -> int:
        nonlocal calls
        calls += 1
        return 5

    handle = build_metrics(pending_entries=_cached(counted).get)

    for _ in range(20):
        handle.render()

    assert calls == 1
    handle.shutdown()


def test_a_gauge_without_a_wired_source_has_no_series() -> None:
    handle = build_metrics()

    assert _GAUGE not in handle.render()[0].decode()
    handle.shutdown()
