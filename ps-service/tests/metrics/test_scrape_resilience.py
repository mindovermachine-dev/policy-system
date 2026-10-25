"""A scrape survives every source failing at once, stays bounded and never moves `/readyz`."""

from __future__ import annotations

import threading
import time
from typing import TYPE_CHECKING

from fastapi.testclient import TestClient

from metrics._app import auth_enabled_config
from metrics._logs import metrics_entries
from metrics._scrape import samples
from ps_service import dependency_health
from ps_service.dependency_health import (
    CELLAR_ELI,
    FALKORDB,
    GRAPH_REPLAY,
    LLM_INTERFACE,
    PASSKEY_SIGNING_POSTGRES,
    STATE_POSTGRES,
    mark_healthy,
    mark_unhealthy,
)
from ps_service.main import create_app
from ps_service.metrics import build_metrics, record_ingestion_run
from ps_service.metrics.catalog import REQUEST_DURATION
from ps_service.metrics.sources import TtlCachedSource

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

_ALL = (FALKORDB, LLM_INTERFACE, CELLAR_ELI, PASSKEY_SIGNING_POSTGRES, STATE_POSTGRES, GRAPH_REPLAY)
_SECRET = "pg-host.internal:5432 password=hunter2 SELECT sum(position)"


def _health_snapshot() -> dict[str, bool]:
    return {name: dependency_health.is_healthy(name) for name in _ALL}


def _broken_counts() -> tuple[int, int]:
    raise RuntimeError(_SECRET)


def _broken_pending() -> int:
    raise ConnectionError(_SECRET)


def _cached(fetch: Callable[[], int], deadline: float = 1.0) -> TtlCachedSource[int]:
    return TtlCachedSource(
        "graph_log_pending_entries",
        fetch,
        ttl_seconds=15.0,
        timeout_seconds=deadline,
    )


def test_every_source_failing_still_serves_the_other_families_without_detail(
    configured_logging: Path,
) -> None:
    handle = build_metrics(
        unhealthy_counts=_broken_counts, pending_entries=_cached(_broken_pending).get
    )
    handle.histogram(REQUEST_DURATION).record(0.01, {"http.route": "/livez"})
    record_ingestion_run("succeeded", 2.0)

    text = handle.render()[0].decode()

    assert "http_server_request_duration_seconds" in text
    assert "ps_service_ingestion_runs_total" in text
    assert "ps_service_dependencies_unhealthy" not in text
    assert "ps_service_graph_log_pending_entries" not in text
    for leaked in ("hunter2", "pg-host", "SELECT", "RuntimeError", "ConnectionError"):
        assert leaked not in text
    handle.shutdown()
    dependency = metrics_entries(configured_logging, "collect_dependencies_unhealthy")
    assert [entry["error_type"] for entry in dependency] == ["RuntimeError"]


def test_a_source_slower_than_its_deadline_does_not_delay_the_scrape_past_it() -> None:
    release = threading.Event()

    def hung() -> int:
        release.wait(timeout=5)
        return 1

    handle = build_metrics(pending_entries=_cached(hung, deadline=0.3).get)

    started = time.monotonic()
    first = handle.render()[0]
    second = handle.render()[0]
    elapsed = time.monotonic() - started
    release.set()

    assert elapsed < 0.3 + 1.0
    assert b"ps_service_graph_log_pending_entries" not in first + second
    handle.shutdown()


def test_twenty_scrapes_against_a_failing_source_call_it_once() -> None:
    calls = 0

    def failing() -> int:
        nonlocal calls
        calls += 1
        raise ConnectionError(_SECRET)

    handle = build_metrics(pending_entries=_cached(failing).get)

    for _ in range(20):
        handle.render()

    assert calls == 1
    handle.shutdown()


def test_a_failing_pending_source_never_changes_the_dependency_registry() -> None:
    mark_healthy(STATE_POSTGRES)
    mark_unhealthy(FALKORDB, error=RuntimeError("down"))
    before = _health_snapshot()
    client = TestClient(create_app(auth_enabled_config(), pending_entries=_broken_pending))

    for _ in range(3):
        assert client.get("/metrics").status_code == 200

    assert _health_snapshot() == before
    assert before[STATE_POSTGRES] is True


def test_the_real_source_against_an_unreachable_postgres_leaves_postgres_health_alone() -> None:
    mark_healthy(STATE_POSTGRES)
    config = auth_enabled_config(state_postgres_host="127.0.0.1", state_postgres_port=59999)
    before = _health_snapshot()
    client = TestClient(create_app(config))

    response = client.get("/metrics")

    assert response.status_code == 200
    assert not samples(response.content, "ps_service_graph_log_pending_entries")
    assert _health_snapshot() == before
