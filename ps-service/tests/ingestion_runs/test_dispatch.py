"""Tests for `ps_service.ingestion_runs.dispatch` (issue #194)."""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

import pytest

from ps_service.ingestion_runs import dispatch
from ps_service.ingestion_runs.errors import (
    IngestionRunAlreadyInProgressError,
    IngestionRunCapacityExceededError,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

_AMPLE_CAP = 5


@pytest.fixture(autouse=True)
def _isolate_dispatch() -> Iterator[None]:  # pyright: ignore[reportUnusedFunction]  # pytest autouse fixture
    dispatch.reset_for_tests()
    yield
    dispatch.reset_for_tests()


def test_work_runs_on_a_different_thread() -> None:
    seen: list[int] = []
    dispatch.reserve_run_slot("r1", short_name="cra", max_in_flight_runs=_AMPLE_CAP)

    dispatch.start_background_run("r1", lambda: seen.append(threading.get_ident()))
    dispatch.wait_for_tests("r1", timeout_seconds=5)

    assert seen
    assert seen[0] != threading.get_ident()


def test_slot_is_held_while_the_work_runs_and_released_after() -> None:
    entered = threading.Event()
    release = threading.Event()

    def _work() -> None:
        entered.set()
        assert release.wait(timeout=5)

    dispatch.reserve_run_slot("r1", short_name="cra", max_in_flight_runs=_AMPLE_CAP)
    assert dispatch.is_run_in_flight("r1")
    dispatch.start_background_run("r1", _work)
    assert entered.wait(timeout=5)

    assert dispatch.is_run_in_flight("r1")
    assert dispatch.in_flight_run_count() == 1

    release.set()
    dispatch.wait_for_tests("r1", timeout_seconds=5)

    assert not dispatch.is_run_in_flight("r1")
    assert dispatch.in_flight_run_count() == 0


@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_slot_is_released_even_when_the_work_raises() -> None:
    def _work() -> None:
        message = "boom"
        raise RuntimeError(message)

    dispatch.reserve_run_slot("r1", short_name="cra", max_in_flight_runs=_AMPLE_CAP)
    dispatch.start_background_run("r1", _work)
    dispatch.wait_for_tests("r1", timeout_seconds=5)

    assert not dispatch.is_run_in_flight("r1")
    assert dispatch.in_flight_run_count() == 0


def test_release_run_slot_is_a_noop_for_an_unknown_run() -> None:
    dispatch.release_run_slot("never-reserved")

    assert dispatch.in_flight_run_count() == 0


def test_two_runs_are_tracked_independently() -> None:
    gate = threading.Event()

    def _hold() -> None:
        gate.wait(timeout=5)

    dispatch.reserve_run_slot("a", short_name="one", max_in_flight_runs=_AMPLE_CAP)
    dispatch.reserve_run_slot("b", short_name="two", max_in_flight_runs=_AMPLE_CAP)
    dispatch.start_background_run("a", _hold)
    dispatch.start_background_run("b", lambda: None)
    dispatch.wait_for_tests("b", timeout_seconds=5)

    assert dispatch.is_run_in_flight("a")
    assert not dispatch.is_run_in_flight("b")
    gate.set()


def test_reset_for_tests_joins_and_clears() -> None:
    done = threading.Event()
    dispatch.reserve_run_slot("r1", short_name="cra", max_in_flight_runs=_AMPLE_CAP)
    dispatch.start_background_run("r1", done.set)

    dispatch.reset_for_tests()

    assert done.is_set()
    assert dispatch.in_flight_run_count() == 0


# --- S6: bounded admission and the same-short_name guard (AC-BI-012, OQ-10) -------------------


def test_a_reserve_over_the_cap_is_rejected_with_the_named_rate_limit_error() -> None:
    dispatch.reserve_run_slot("a", short_name="one", max_in_flight_runs=1)

    with pytest.raises(IngestionRunCapacityExceededError) as raised:
        dispatch.reserve_run_slot("b", short_name="two", max_in_flight_runs=1)

    assert str(raised.value) == (
        "too many ingestion runs are already in progress (limit 1); "
        "wait for one to finish, then try again"
    )
    assert dispatch.in_flight_run_count() == 1
    assert not dispatch.is_run_in_flight("b")


def test_releasing_a_slot_lets_the_next_reserve_succeed() -> None:
    dispatch.reserve_run_slot("a", short_name="one", max_in_flight_runs=1)
    dispatch.release_run_slot("a")

    dispatch.reserve_run_slot("b", short_name="two", max_in_flight_runs=1)

    assert dispatch.is_run_in_flight("b")


def test_racing_reserves_admit_exactly_the_cap() -> None:
    cap = 3
    threads_count = 8
    barrier = threading.Barrier(threads_count)
    outcomes: list[bool] = []
    outcomes_lock = threading.Lock()

    def _reserve(index: int) -> None:
        barrier.wait(timeout=5)
        try:
            dispatch.reserve_run_slot(f"r{index}", short_name=f"s{index}", max_in_flight_runs=cap)
        except IngestionRunCapacityExceededError:
            admitted = False
        else:
            admitted = True
        with outcomes_lock:
            outcomes.append(admitted)

    workers = [threading.Thread(target=_reserve, args=(i,)) for i in range(threads_count)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=5)

    assert outcomes.count(True) == cap
    assert outcomes.count(False) == threads_count - cap
    assert dispatch.in_flight_run_count() == cap


def test_a_second_reserve_for_the_same_short_name_is_rejected_then_allowed_after_release() -> None:
    dispatch.reserve_run_slot("a", short_name="cra", max_in_flight_runs=5)

    with pytest.raises(IngestionRunAlreadyInProgressError) as raised:
        dispatch.reserve_run_slot("b", short_name="cra", max_in_flight_runs=5)

    assert str(raised.value) == (
        "an ingestion run for short_name 'cra' is already in progress; "
        "wait for it to finish instead of submitting it again"
    )
    assert dispatch.in_flight_run_count() == 1

    dispatch.release_run_slot("a")
    dispatch.reserve_run_slot("b", short_name="cra", max_in_flight_runs=5)
    assert dispatch.is_run_in_flight("b")


def test_the_duplicate_short_name_check_runs_before_the_cap_check() -> None:
    dispatch.reserve_run_slot("a", short_name="cra", max_in_flight_runs=1)

    with pytest.raises(IngestionRunAlreadyInProgressError):
        dispatch.reserve_run_slot("b", short_name="cra", max_in_flight_runs=1)
