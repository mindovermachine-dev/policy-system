"""Ingestion run outcome metrics are recorded once per run, by the call that won the write."""

from __future__ import annotations

import dataclasses
import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, cast

import pytest
from company_merge._thresholds import REVIEW_THRESHOLD, ROLE_REVIEW_THRESHOLD
from ingestion_runs._fakes import InMemoryIngestionRunStore
from metrics._scrape import samples

from ps_service.config import ServiceConfig
from ps_service.logging import facade
from ps_service.mcp_interface import ingestion_tools as mcp_server
from ps_service.metrics import build_metrics

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from ps_service.metrics import MetricsHandle

_RUNS = "ps_service_ingestion_runs_total"
_DURATION_COUNT = "ps_service_ingestion_run_duration_seconds_count"
_DURATION_SUM = "ps_service_ingestion_run_duration_seconds_sum"
_CELEX = "32022R2065"


@pytest.fixture(autouse=True)
def _configured_logging(  # pyright: ignore[reportUnusedFunction]  # pytest autouse fixture — invoked by name-collection, never referenced in-module
    tmp_path: Path,
) -> Iterator[None]:
    """Install a real default emitter, as the service has after startup, restored afterwards."""
    saved = facade._atexit_registered  # pyright: ignore[reportPrivateUsage]
    facade.configure(log_path=tmp_path / "ingestion-metrics.jsonl")
    yield
    facade.reset_for_tests()
    facade._atexit_registered = saved  # pyright: ignore[reportPrivateUsage]


@pytest.fixture
def handle() -> Iterator[MetricsHandle]:
    metrics = build_metrics()
    yield metrics
    metrics.shutdown()


def _runs(metrics: MetricsHandle) -> dict[str, float]:
    return {s.labels["outcome"]: s.value for s in samples(metrics.render()[0], _RUNS)}


def _duration_observations(metrics: MetricsHandle) -> float:
    return sum(s.value for s in samples(metrics.render()[0], _DURATION_COUNT))


def _submitted(store: InMemoryIngestionRunStore) -> str:
    run_id = str(uuid.uuid4())
    store.create_run(run_id=run_id, celex=_CELEX, short_name="dsa", actor=("sub", "iss"))
    return run_id


def _record(store: InMemoryIngestionRunStore, run_id: str, result: dict[str, object] | str) -> None:
    mcp_server._record_ingestion_run_outcome(  # pyright: ignore[reportPrivateUsage]
        store, run_id=run_id, result=result, reason_code=None, duration_seconds=3.0
    )


def test_a_winning_success_write_counts_one_succeeded_run(handle: MetricsHandle) -> None:
    store = InMemoryIngestionRunStore()

    _record(store, _submitted(store), {"new_obligations": 1})

    assert _runs(handle) == {"succeeded": 1.0}
    assert _duration_observations(handle) == 1.0
    assert [s.value for s in samples(handle.render()[0], _DURATION_SUM)] == [3.0]


def test_a_winning_failure_write_counts_one_failed_run(handle: MetricsHandle) -> None:
    store = InMemoryIngestionRunStore()

    _record(store, _submitted(store), "error: the pipeline failed")

    assert _runs(handle) == {"failed": 1.0}


def test_a_write_that_loses_the_race_records_nothing(handle: MetricsHandle) -> None:
    store = InMemoryIngestionRunStore()
    run_id = _submitted(store)
    assert store.complete_run(run_id, status="succeeded", result={}, error=None) is True

    _record(store, run_id, "error: late failure")

    assert _runs(handle) == {}
    assert _duration_observations(handle) == 0.0


def test_a_failed_terminal_write_records_nothing(handle: MetricsHandle) -> None:
    store = InMemoryIngestionRunStore()
    run_id = _submitted(store)
    store.fail_next_complete = 1

    _record(store, run_id, {"new_obligations": 1})

    assert _runs(handle) == {}


def test_the_scrape_carries_no_run_or_instrument_identifier(handle: MetricsHandle) -> None:
    store = InMemoryIngestionRunStore()
    run_id = _submitted(store)

    _record(store, run_id, {"new_obligations": 1})

    body = handle.render()[0].decode()
    assert run_id not in body
    assert _CELEX not in body


def test_reconciling_an_orphaned_run_counts_one_failed_run_with_its_age(
    handle: MetricsHandle,
) -> None:
    store = InMemoryIngestionRunStore()
    run_id = _submitted(store)
    row = dataclasses.replace(
        store.rows[run_id], submitted_at=datetime.now(UTC) - timedelta(seconds=90)
    )
    store.rows[run_id] = row

    mcp_server._reconcile_orphaned_run(store, row)  # pyright: ignore[reportPrivateUsage]
    mcp_server._reconcile_orphaned_run(store, store.rows[run_id])  # pyright: ignore[reportPrivateUsage]

    assert _runs(handle) == {"failed": 1.0}
    [total] = [s.value for s in samples(handle.render()[0], _DURATION_SUM)]
    assert 90.0 <= total < 120.0


def test_a_reconciler_that_loses_the_race_or_fails_records_nothing(
    handle: MetricsHandle,
) -> None:
    store = InMemoryIngestionRunStore()
    lost = _submitted(store)
    stale_row = store.rows[lost]
    store.complete_run(lost, status="succeeded", result={}, error=None)
    broken = _submitted(store)

    mcp_server._reconcile_orphaned_run(store, stale_row)  # pyright: ignore[reportPrivateUsage]
    store.fail_next_complete = 1
    mcp_server._reconcile_orphaned_run(store, store.rows[broken])  # pyright: ignore[reportPrivateUsage]

    assert _runs(handle) == {}


def test_a_submission_time_in_the_future_is_clamped_to_a_zero_duration(
    handle: MetricsHandle,
) -> None:
    store = InMemoryIngestionRunStore()
    run_id = _submitted(store)
    row = dataclasses.replace(
        store.rows[run_id], submitted_at=datetime.now(UTC) + timedelta(hours=1)
    )
    store.rows[run_id] = row

    mcp_server._reconcile_orphaned_run(store, row)  # pyright: ignore[reportPrivateUsage]

    assert [s.value for s in samples(handle.render()[0], _DURATION_SUM)] == [0.0]


def _config() -> ServiceConfig:
    return ServiceConfig(
        host="127.0.0.1",
        port=8000,
        graceful_shutdown_seconds=10,
        logging_dir=None,
        llm_interface_model="azure/gpt-4o",
        llm_interface_embed_model="azure/text-embedding-3-large",
        company_merge_capability_merge_threshold=0.83,
        company_merge_capability_review_threshold=REVIEW_THRESHOLD,
        company_merge_role_review_threshold=ROLE_REVIEW_THRESHOLD,
        is_local_test_bypass_active=True,
    )


@pytest.mark.parametrize(
    ("pipeline_result", "outcome"),
    [({"new_obligations": 1}, "succeeded"), ("error: the pipeline failed", "failed")],
)
def test_the_background_worker_records_the_run_it_completed(
    handle: MetricsHandle,
    monkeypatch: pytest.MonkeyPatch,
    pipeline_result: dict[str, object] | str,
    outcome: str,
) -> None:
    def pipeline(*_args: object, **_kwargs: object) -> dict[str, object] | str:
        return pipeline_result

    # detroit-exception: legitimate fault-injection carve-out, see
    # `test_failure_detail_scrubbing.py`; the real pipeline needs the full graph and LLM stack,
    # and only the recording after the terminal write is under test.
    monkeypatch.setattr(mcp_server, "_run_prepared_catalog_ingestion", pipeline)
    store = InMemoryIngestionRunStore()

    mcp_server._execute_background_ingestion(  # pyright: ignore[reportPrivateUsage]
        cast("mcp_server._PreparedCatalogIngestion", object()),  # pyright: ignore[reportPrivateUsage]
        run_id=_submitted(store),
        config=_config(),
        store=store,
    )

    assert _runs(handle) == {outcome: 1.0}
    [total] = [s.value for s in samples(handle.render()[0], _DURATION_SUM)]
    assert 0.0 <= total < 10.0
