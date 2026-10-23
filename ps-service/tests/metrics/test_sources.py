"""`TtlCachedSource`: a bounded, cached, single-flight wrapper around a slow metric source."""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING

from metrics._logs import metrics_entries
from ps_service.metrics.sources import TtlCachedSource

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

_TTL = 15.0
_DEADLINE = 0.2


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class _CountingSource:
    def __init__(self, value: int = 7) -> None:
        self.calls = 0
        self.value = value

    def __call__(self) -> int:
        self.calls += 1
        return self.value


def _source(
    fetch: Callable[[], int], clock: _Clock, deadline: float = _DEADLINE
) -> TtlCachedSource[int]:
    return TtlCachedSource(
        "probe",
        fetch,
        ttl_seconds=_TTL,
        timeout_seconds=deadline,
        clock=clock,
    )


def test_a_second_call_within_the_ttl_does_not_call_the_source() -> None:
    fetch, clock = _CountingSource(), _Clock()
    source = _source(fetch, clock)

    assert (source.get(), source.get()) == (7, 7)
    assert fetch.calls == 1


def test_the_source_is_called_again_once_the_ttl_has_passed() -> None:
    fetch, clock = _CountingSource(), _Clock()
    source = _source(fetch, clock)
    source.get()

    clock.now += _TTL
    fetch.value = 9

    assert source.get() == 9
    assert fetch.calls == 2


def test_a_failure_is_cached_for_the_ttl_and_reads_as_no_value(
    configured_logging: Path,
) -> None:
    clock = _Clock()
    calls = 0

    def failing() -> int:
        nonlocal calls
        calls += 1
        raise ConnectionError("db.internal:5432 refused")

    source = _source(failing, clock)

    assert [source.get() for _ in range(5)] == [None] * 5
    assert calls == 1
    clock.now += _TTL
    assert source.get() is None
    assert calls == 2
    entries = metrics_entries(configured_logging, "collect_probe")
    assert [entry["error_type"] for entry in entries] == ["ConnectionError", "ConnectionError"]
    assert all("db.internal" not in str(entry) for entry in entries)


def test_a_source_that_outlasts_the_deadline_reads_as_no_value_without_blocking_again(
    configured_logging: Path,
) -> None:
    clock, release = _Clock(), threading.Event()
    calls = 0

    def blocking() -> int:
        nonlocal calls
        calls += 1
        release.wait(timeout=5)
        return 3

    source = _source(blocking, clock)

    started = time.monotonic()
    first = source.get()
    first_elapsed = time.monotonic() - started
    clock.now += _TTL
    started = time.monotonic()
    second = source.get()
    second_elapsed = time.monotonic() - started
    release.set()

    assert first is None
    assert 0.1 <= first_elapsed < 2.0
    assert second is None
    assert second_elapsed < 0.1
    assert calls == 1
    [entry] = metrics_entries(configured_logging, "collect_probe")
    assert entry["error_type"] == "SourceDeadlineExceededError"


def test_the_source_is_called_again_after_the_hung_call_returns() -> None:
    clock, release = _Clock(), threading.Event()
    values = iter([1, 2])

    def sometimes_slow() -> int:
        value = next(values)
        if value == 1:
            release.wait(timeout=5)
        return value

    source = _source(sometimes_slow, clock)
    assert source.get() is None

    release.set()
    time.sleep(0.1)

    assert source.get() == 1
    clock.now += _TTL
    assert source.get() == 2


def test_concurrent_callers_share_one_source_call() -> None:
    fetch, clock, gate = _CountingSource(), _Clock(), threading.Event()

    def slow() -> int:
        gate.wait(timeout=2)
        return fetch()

    source = _source(slow, clock, deadline=2.0)
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(source.get) for _ in range(8)]
        time.sleep(0.05)
        gate.set()
        results = [future.result(timeout=5) for future in futures]

    assert results == [7] * 8
    assert fetch.calls == 1
