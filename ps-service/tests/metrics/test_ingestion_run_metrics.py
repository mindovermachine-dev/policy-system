"""Recording ingestion run outcomes into the process metrics slot."""

from __future__ import annotations

from metrics._scrape import label_values, samples
from ps_service.metrics import build_metrics, record_ingestion_run

_SCOPE_LABELS = frozenset({"otel_scope_name", "otel_scope_version", "otel_scope_schema_url"})
_RUNS = "ps_service_ingestion_runs_total"
_DURATION_COUNT = "ps_service_ingestion_run_duration_seconds_count"
_DURATION_SUM = "ps_service_ingestion_run_duration_seconds_sum"


def _runs(body: bytes) -> dict[str, float]:
    return {s.labels["outcome"]: s.value for s in samples(body, _RUNS)}


def test_a_recorded_run_counts_once_and_observes_its_duration() -> None:
    handle = build_metrics()

    record_ingestion_run("succeeded", 12.5)

    body = handle.render()[0]
    assert _runs(body) == {"succeeded": 1.0}
    assert [s.value for s in samples(body, _DURATION_COUNT)] == [1.0]
    assert [s.value for s in samples(body, _DURATION_SUM)] == [12.5]
    handle.shutdown()


def test_outcomes_are_counted_separately_and_the_only_label_is_outcome() -> None:
    handle = build_metrics()

    record_ingestion_run("succeeded", 1.0)
    record_ingestion_run("failed", 2.0)
    record_ingestion_run("failed", 3.0)

    body = handle.render()[0]
    assert _runs(body) == {"succeeded": 1.0, "failed": 2.0}
    assert label_values(body, _DURATION_COUNT, "outcome") == {"succeeded", "failed"}
    label_names = {key for s in samples(body, _RUNS) for key in s.labels}
    assert label_names - _SCOPE_LABELS == {"outcome"}
    handle.shutdown()


def test_a_negative_duration_is_recorded_as_zero() -> None:
    handle = build_metrics()

    record_ingestion_run("failed", -4.0)

    assert [s.value for s in samples(handle.render()[0], _DURATION_SUM)] == [0.0]
    handle.shutdown()


def test_recording_without_an_installed_handle_is_a_no_op() -> None:
    record_ingestion_run("succeeded", 1.0)


def test_a_stopped_older_app_does_not_unhook_a_newer_one() -> None:
    older = build_metrics()
    newer = build_metrics()

    older.shutdown()
    record_ingestion_run("succeeded", 1.0)

    assert _runs(newer.render()[0]) == {"succeeded": 1.0}
    newer.shutdown()


def test_recording_stops_after_the_installed_handle_shuts_down() -> None:
    handle = build_metrics()
    handle.shutdown()

    record_ingestion_run("succeeded", 1.0)

    assert _runs(handle.render()[0]) == {}
