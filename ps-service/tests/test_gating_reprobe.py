"""Unit tests for `ps_service.gating_reprobe.GatingReprobe`.

The probes are plain callables injected into the harness, so these tests substitute nothing: they
count calls, block on events and read a registry-shaped health callable. The harness never records
health itself; a probe that records its own outcome is modelled by the probe writing to the same
dict the injected `is_healthy` reads.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from typing import TYPE_CHECKING

import pytest

from ps_service import dependency_health
from ps_service.config import ServiceConfig
from ps_service.dependency_health import (
    CELLAR_ELI,
    FALKORDB,
    GRAPH_REPLAY,
    LLM_INTERFACE,
    PASSKEY_SIGNING_POSTGRES,
    STATE_POSTGRES,
)
from ps_service.gating_reprobe import GatingReprobe
from ps_service.logging.facade import configure, reset_for_tests
from ps_service.main import (
    _GATING_DEPENDENCIES,  # pyright: ignore[reportPrivateUsage]  # the set the harness is wired to
    _all_dependency_probes,  # pyright: ignore[reportPrivateUsage]  # the probe table the harness reads
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

_ALL_TRACKED = (
    FALKORDB,
    LLM_INTERFACE,
    CELLAR_ELI,
    PASSKEY_SIGNING_POSTGRES,
    STATE_POSTGRES,
    GRAPH_REPLAY,
)
_WAIT_SECONDS = 5.0
_CONFIG = ServiceConfig(host="127.0.0.1", port=8000, graceful_shutdown_seconds=10, logging_dir=None)


class _Rig:
    """Health table, per-dependency call counters and recording probes for one harness."""

    def __init__(self, *, unhealthy: tuple[str, ...]) -> None:
        self.healthy = {name: name not in unhealthy for name in _ALL_TRACKED}
        self.calls: dict[str, int] = dict.fromkeys(_ALL_TRACKED, 0)
        self.release = threading.Event()
        self.entered = threading.Event()

    def probe_recovering(self, name: str) -> Callable[[], None]:
        def probe() -> None:
            self.calls[name] += 1
            self.healthy[name] = True

        return probe

    def probe_blocking(self, name: str) -> Callable[[], None]:
        def probe() -> None:
            self.calls[name] += 1
            self.entered.set()
            assert self.release.wait(_WAIT_SECONDS)
            self.healthy[name] = True

        return probe

    def harness(
        self,
        probes: dict[str, Callable[[], None]],
        *,
        dependencies: tuple[str, ...] = _GATING_DEPENDENCIES,
        min_interval_seconds: float = 0.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> GatingReprobe:
        return GatingReprobe(
            dependencies=lambda: dependencies,
            probe_for=probes.get,
            is_healthy=lambda name: self.healthy[name],
            min_interval_seconds=min_interval_seconds,
            clock=clock,
        )

    def probe_failing(self, name: str) -> Callable[[], None]:
        def probe() -> None:
            self.calls[name] += 1
            raise ConnectionError("still down")

        return probe


async def _poll(reprobe: GatingReprobe, *, deadline_seconds: float) -> float:
    """One `/ready`-shaped poll; returns how long it took."""
    loop = asyncio.get_running_loop()
    started = loop.time()
    await reprobe.refresh(started + deadline_seconds)
    return loop.time() - started


def test_refresh_probes_only_the_unhealthy_gating_dependencies() -> None:
    rig = _Rig(unhealthy=(FALKORDB, STATE_POSTGRES))
    reprobe = rig.harness({name: rig.probe_recovering(name) for name in _ALL_TRACKED})

    asyncio.run(_poll(reprobe, deadline_seconds=_WAIT_SECONDS))

    assert {name: count for name, count in rig.calls.items() if count} == {
        FALKORDB: 1,
        STATE_POSTGRES: 1,
    }


def test_refresh_never_probes_replay_or_the_non_gating_dependencies() -> None:
    rig = _Rig(unhealthy=_ALL_TRACKED)
    tabled = [name for name, _ in _all_dependency_probes(_CONFIG)]
    reprobe = rig.harness({name: rig.probe_recovering(name) for name in tabled})

    asyncio.run(_poll(reprobe, deadline_seconds=_WAIT_SECONDS))

    for excluded in (GRAPH_REPLAY, LLM_INTERFACE, CELLAR_ELI, PASSKEY_SIGNING_POSTGRES):
        assert rig.calls[excluded] == 0, excluded


def test_refresh_skips_a_gating_dependency_that_has_no_probe() -> None:
    rig = _Rig(unhealthy=(GRAPH_REPLAY, FALKORDB))
    reprobe = rig.harness({FALKORDB: rig.probe_recovering(FALKORDB)})

    asyncio.run(_poll(reprobe, deadline_seconds=_WAIT_SECONDS))

    assert rig.calls[GRAPH_REPLAY] == 0
    assert rig.calls[FALKORDB] == 1


def test_refresh_probes_nothing_when_every_gating_dependency_is_healthy() -> None:
    rig = _Rig(unhealthy=(LLM_INTERFACE, CELLAR_ELI))
    reprobe = rig.harness({name: rig.probe_recovering(name) for name in _ALL_TRACKED})

    elapsed = asyncio.run(_poll(reprobe, deadline_seconds=_WAIT_SECONDS))

    assert sum(rig.calls.values()) == 0
    assert elapsed < 0.5


def test_refresh_probes_only_the_still_unhealthy_one_when_the_other_has_recovered() -> None:
    rig = _Rig(unhealthy=(FALKORDB,))
    reprobe = rig.harness({name: rig.probe_recovering(name) for name in _ALL_TRACKED})

    asyncio.run(_poll(reprobe, deadline_seconds=_WAIT_SECONDS))

    assert rig.calls[FALKORDB] == 1
    assert rig.calls[STATE_POSTGRES] == 0


def test_refresh_does_not_write_the_registry() -> None:
    dependency_health.mark_unhealthy(FALKORDB, error=RuntimeError("down"))
    calls: list[str] = []
    reprobe = GatingReprobe(
        dependencies=lambda: (FALKORDB,),
        probe_for=lambda name: lambda: calls.append(name),
        is_healthy=dependency_health.is_healthy,
        min_interval_seconds=0.0,
        clock=time.monotonic,
    )

    asyncio.run(_poll(reprobe, deadline_seconds=_WAIT_SECONDS))

    assert calls == [FALKORDB]
    assert dependency_health.is_healthy(FALKORDB) is False


def test_refresh_does_not_raise_when_a_probe_raises() -> None:
    rig = _Rig(unhealthy=(FALKORDB,))

    def failing() -> None:
        raise ConnectionError("secret-host-1234 refused")

    reprobe = rig.harness({FALKORDB: failing})

    asyncio.run(_poll(reprobe, deadline_seconds=_WAIT_SECONDS))

    assert rig.healthy[FALKORDB] is False


def test_concurrent_polls_share_one_probe_per_unhealthy_dependency() -> None:
    rig = _Rig(unhealthy=(FALKORDB, STATE_POSTGRES))
    reprobe = rig.harness({name: rig.probe_blocking(name) for name in _ALL_TRACKED})

    async def polls() -> None:
        tasks = [
            asyncio.ensure_future(_poll(reprobe, deadline_seconds=_WAIT_SECONDS)) for _ in range(20)
        ]
        await asyncio.sleep(0.2)
        rig.release.set()
        await asyncio.gather(*tasks)

    asyncio.run(polls())

    assert rig.calls[FALKORDB] == 1
    assert rig.calls[STATE_POSTGRES] == 1


class _FakeClock:
    """A settable monotonic clock."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_first_attempt_is_not_rate_limited() -> None:
    rig = _Rig(unhealthy=(FALKORDB,))
    clock = _FakeClock()
    reprobe = rig.harness(
        {FALKORDB: rig.probe_failing(FALKORDB)}, min_interval_seconds=100.0, clock=clock
    )

    asyncio.run(_poll(reprobe, deadline_seconds=_WAIT_SECONDS))

    assert rig.calls[FALKORDB] == 1


def test_polls_inside_the_minimum_interval_do_not_probe_again() -> None:
    rig = _Rig(unhealthy=(FALKORDB,))
    clock = _FakeClock()
    reprobe = rig.harness(
        {FALKORDB: rig.probe_failing(FALKORDB)}, min_interval_seconds=5.0, clock=clock
    )

    async def polls() -> None:
        for now in (0.0, 1.0, 4.9):
            clock.now = now
            await _poll(reprobe, deadline_seconds=_WAIT_SECONDS)

    asyncio.run(polls())

    assert rig.calls[FALKORDB] == 1


def test_a_poll_after_the_interval_elapses_probes_again() -> None:
    rig = _Rig(unhealthy=(FALKORDB,))
    clock = _FakeClock()
    reprobe = rig.harness(
        {FALKORDB: rig.probe_failing(FALKORDB)}, min_interval_seconds=5.0, clock=clock
    )

    async def polls() -> None:
        for now in (0.0, 4.9, 5.0, 9.9, 10.0):
            clock.now = now
            await _poll(reprobe, deadline_seconds=_WAIT_SECONDS)

    asyncio.run(polls())

    assert rig.calls[FALKORDB] == 3


def test_the_interval_is_kept_per_dependency() -> None:
    rig = _Rig(unhealthy=(FALKORDB,))
    clock = _FakeClock()
    reprobe = rig.harness(
        {name: rig.probe_failing(name) for name in _ALL_TRACKED},
        min_interval_seconds=5.0,
        clock=clock,
    )

    async def polls() -> None:
        await _poll(reprobe, deadline_seconds=_WAIT_SECONDS)
        clock.now = 1.0
        rig.healthy[STATE_POSTGRES] = False
        await _poll(reprobe, deadline_seconds=_WAIT_SECONDS)

    asyncio.run(polls())

    assert rig.calls[FALKORDB] == 1
    assert rig.calls[STATE_POSTGRES] == 1


def test_a_fast_failing_flight_blocks_the_next_probe_until_the_interval_ends() -> None:
    rig = _Rig(unhealthy=(FALKORDB,))
    clock = _FakeClock()
    reprobe = rig.harness(
        {FALKORDB: rig.probe_failing(FALKORDB)}, min_interval_seconds=5.0, clock=clock
    )

    async def polls() -> list[int]:
        seen: list[int] = []
        for now in (0.0, 3.0, 5.0):
            clock.now = now
            await _poll(reprobe, deadline_seconds=_WAIT_SECONDS)
            seen.append(rig.calls[FALKORDB])
        return seen

    assert asyncio.run(polls()) == [1, 1, 2]


def test_a_flight_that_ends_after_the_interval_lets_the_next_poll_probe_at_once() -> None:
    rig = _Rig(unhealthy=(FALKORDB,))
    clock = _FakeClock()
    calls_before_release = threading.Event()

    def slow_then_failing() -> None:
        rig.calls[FALKORDB] += 1
        if rig.calls[FALKORDB] == 1:
            calls_before_release.set()
            assert rig.release.wait(_WAIT_SECONDS)
        raise ConnectionError("still down")

    reprobe = rig.harness({FALKORDB: slow_then_failing}, min_interval_seconds=5.0, clock=clock)

    async def polls() -> None:
        await _poll(reprobe, deadline_seconds=0.1)
        assert calls_before_release.is_set()
        clock.now = 10.0
        rig.release.set()
        for _ in range(100):
            await _poll(reprobe, deadline_seconds=0.05)
            if rig.calls[FALKORDB] == 2:
                return

    try:
        asyncio.run(polls())
    finally:
        rig.release.set()

    assert rig.calls[FALKORDB] == 2


def test_a_double_hang_costs_one_deadline_not_two() -> None:
    rig = _Rig(unhealthy=(FALKORDB, STATE_POSTGRES))
    reprobe = rig.harness({name: rig.probe_blocking(name) for name in _ALL_TRACKED})
    try:
        elapsed = asyncio.run(_poll(reprobe, deadline_seconds=0.3))
    finally:
        rig.release.set()

    assert 0.25 <= elapsed < 0.55
    assert rig.healthy[FALKORDB] is False
    assert rig.healthy[STATE_POSTGRES] is False


def test_a_hung_probe_keeps_its_slot_so_later_polls_start_no_second_probe() -> None:
    rig = _Rig(unhealthy=(FALKORDB,))
    reprobe = rig.harness({FALKORDB: rig.probe_blocking(FALKORDB)})

    async def polls() -> None:
        try:
            for _ in range(3):
                await _poll(reprobe, deadline_seconds=0.1)
        finally:
            rig.release.set()

    asyncio.run(polls())

    assert rig.calls[FALKORDB] == 1


def test_a_late_success_after_the_deadline_still_lands() -> None:
    rig = _Rig(unhealthy=(FALKORDB,))
    reprobe = rig.harness({FALKORDB: rig.probe_blocking(FALKORDB)})

    asyncio.run(_poll(reprobe, deadline_seconds=0.1))
    assert rig.healthy[FALKORDB] is False
    rig.release.set()
    deadline = time.monotonic() + _WAIT_SECONDS
    while not rig.healthy[FALKORDB] and time.monotonic() < deadline:
        time.sleep(0.01)

    assert rig.healthy[FALKORDB] is True


def test_a_cancelled_poll_does_not_fail_the_flight_or_other_polls() -> None:
    rig = _Rig(unhealthy=(FALKORDB,))
    reprobe = rig.harness({FALKORDB: rig.probe_blocking(FALKORDB)})
    loop_errors: list[dict[str, object]] = []

    def record_loop_error(loop: asyncio.AbstractEventLoop, context: dict[str, object]) -> None:
        del loop
        loop_errors.append(context)

    async def polls() -> float:
        asyncio.get_running_loop().set_exception_handler(record_loop_error)
        first = asyncio.ensure_future(_poll(reprobe, deadline_seconds=_WAIT_SECONDS))
        second = asyncio.ensure_future(_poll(reprobe, deadline_seconds=_WAIT_SECONDS))
        await asyncio.sleep(0.1)
        first.cancel()
        await asyncio.sleep(0)
        rig.release.set()
        elapsed = await second
        await asyncio.sleep(0.1)
        return elapsed

    elapsed = asyncio.run(polls())

    assert elapsed < _WAIT_SECONDS
    assert rig.healthy[FALKORDB] is True
    assert rig.calls[FALKORDB] == 1
    assert loop_errors == []


@pytest.fixture
def log_path(tmp_path: Path) -> Iterator[Path]:
    """Route the real Logging facade to a file for the test, then restore the process default."""
    path = tmp_path / "ps-service.jsonl"
    configure(log_path=path)
    yield path
    reset_for_tests()


def _reprobe_entries(path: Path) -> list[dict[str, object]]:
    """Drain the emitter and return the `ready_reprobe` entries written so far."""
    reset_for_tests()
    lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    return [line for line in lines if line.get("action") == "ready_reprobe"]


def test_one_failed_entry_per_outage_however_many_polls_fail(log_path: Path) -> None:
    rig = _Rig(unhealthy=(FALKORDB,))
    reprobe = rig.harness({FALKORDB: rig.probe_failing(FALKORDB)})

    for _ in range(5):
        asyncio.run(_poll(reprobe, deadline_seconds=_WAIT_SECONDS))

    entries = _reprobe_entries(log_path)
    assert rig.calls[FALKORDB] == 5
    assert len(entries) == 1
    assert entries[0]["component"] == "entrypoint"
    assert entries[0]["outcome"] == "failed"
    assert entries[0]["dependency"] == FALKORDB
    assert entries[0]["error_class"] == "ConnectionError"
    assert "still down" not in json.dumps(entries[0])


def test_one_recovery_entry_when_a_reprobe_succeeds_after_a_logged_outage(log_path: Path) -> None:
    rig = _Rig(unhealthy=(FALKORDB,))
    probes = {FALKORDB: rig.probe_failing(FALKORDB)}
    reprobe = rig.harness(probes)
    asyncio.run(_poll(reprobe, deadline_seconds=_WAIT_SECONDS))
    probes[FALKORDB] = rig.probe_recovering(FALKORDB)

    asyncio.run(_poll(reprobe, deadline_seconds=_WAIT_SECONDS))
    asyncio.run(_poll(reprobe, deadline_seconds=_WAIT_SECONDS))

    entries = _reprobe_entries(log_path)
    assert [entry["outcome"] for entry in entries] == ["failed", "succeeded"]
    assert entries[1]["dependency"] == FALKORDB


def test_recovery_entry_when_real_traffic_heals_the_dependency_between_polls(
    log_path: Path,
) -> None:
    rig = _Rig(unhealthy=(FALKORDB,))
    reprobe = rig.harness({FALKORDB: rig.probe_failing(FALKORDB)})
    asyncio.run(_poll(reprobe, deadline_seconds=_WAIT_SECONDS))
    rig.healthy[FALKORDB] = True

    asyncio.run(_poll(reprobe, deadline_seconds=_WAIT_SECONDS))
    asyncio.run(_poll(reprobe, deadline_seconds=_WAIT_SECONDS))

    entries = _reprobe_entries(log_path)
    assert [entry["outcome"] for entry in entries] == ["failed", "succeeded"]
    assert rig.calls[FALKORDB] == 1


def test_a_second_outage_after_recovery_logs_again(log_path: Path) -> None:
    rig = _Rig(unhealthy=(FALKORDB,))
    failing = rig.harness({FALKORDB: rig.probe_failing(FALKORDB)})
    asyncio.run(_poll(failing, deadline_seconds=_WAIT_SECONDS))
    rig.healthy[FALKORDB] = True
    asyncio.run(_poll(failing, deadline_seconds=_WAIT_SECONDS))
    rig.healthy[FALKORDB] = False

    asyncio.run(_poll(failing, deadline_seconds=_WAIT_SECONDS))
    asyncio.run(_poll(failing, deadline_seconds=_WAIT_SECONDS))

    entries = _reprobe_entries(log_path)
    assert [entry["outcome"] for entry in entries] == ["failed", "succeeded", "failed"]


def test_timeout_is_logged_with_the_class_name_timeouterror(log_path: Path) -> None:
    rig = _Rig(unhealthy=(FALKORDB,))
    reprobe = rig.harness({FALKORDB: rig.probe_blocking(FALKORDB)})
    try:
        asyncio.run(_poll(reprobe, deadline_seconds=0.1))
        asyncio.run(_poll(reprobe, deadline_seconds=0.1))
    finally:
        rig.release.set()

    entries = _reprobe_entries(log_path)
    assert len(entries) == 1
    assert entries[0]["outcome"] == "failed"
    assert entries[0]["error_class"] == "TimeoutError"


def test_a_late_success_after_a_logged_timeout_logs_one_recovery(log_path: Path) -> None:
    rig = _Rig(unhealthy=(FALKORDB,))
    reprobe = rig.harness({FALKORDB: rig.probe_blocking(FALKORDB)})
    asyncio.run(_poll(reprobe, deadline_seconds=0.1))
    rig.release.set()
    deadline = time.monotonic() + _WAIT_SECONDS
    while not rig.healthy[FALKORDB] and time.monotonic() < deadline:
        time.sleep(0.01)
    asyncio.run(_poll(reprobe, deadline_seconds=_WAIT_SECONDS))

    entries = _reprobe_entries(log_path)
    assert [entry["outcome"] for entry in entries] == ["failed", "succeeded"]


def test_a_poll_with_no_configured_log_emitter_still_answers() -> None:
    reset_for_tests()
    rig = _Rig(unhealthy=(FALKORDB,))
    reprobe = rig.harness({FALKORDB: rig.probe_failing(FALKORDB)})

    asyncio.run(_poll(reprobe, deadline_seconds=_WAIT_SECONDS))

    assert rig.healthy[FALKORDB] is False
