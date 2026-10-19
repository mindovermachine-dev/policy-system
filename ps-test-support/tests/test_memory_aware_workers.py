"""Proves `memory_aware_workers` sizes `-n auto` by available memory as well as CPUs.

The policy and the memory reader are driven with memory figures and with throwaway
`/proc` and cgroup trees in `tmp_path`. The hook is driven through a real `python -m pytest`
subprocess against a throwaway project, and the worker count is read from xdist's own
`created: N/N workers` line.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from typing import TYPE_CHECKING

import pytest

from ps_test_support.memory_aware_workers import (
    PER_WORKER_MEMORY_BUDGET_BYTES,
    available_memory_bytes,
    worker_count,
)

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

MIB = 1024 * 1024
GIB = 1024 * MIB
SUBPROCESS_TIMEOUT_SECONDS = 120.0
PLUGIN_MODULE = "ps_test_support.memory_aware_workers"


# --- policy: memory figures in, worker count out ------------------------------


def test_plentiful_memory_keeps_the_cpu_worker_count() -> None:
    assert worker_count(8, 64 * GIB, per_worker_bytes=800 * MIB) == 8


def test_scarce_memory_caps_workers_at_whole_budgets_that_fit() -> None:
    assert worker_count(8, 4 * GIB + 700 * MIB, per_worker_bytes=800 * MIB) == 5


def test_memory_below_one_budget_still_runs_one_worker() -> None:
    assert worker_count(8, 100 * MIB, per_worker_bytes=800 * MIB) == 1


def test_unknown_memory_keeps_the_cpu_worker_count() -> None:
    assert worker_count(8, None, per_worker_bytes=800 * MIB) == 8


def test_default_budget_is_the_per_worker_memory_budget() -> None:
    available = 3 * PER_WORKER_MEMORY_BUDGET_BYTES

    assert worker_count(8, available) == 3


# --- reading available memory -------------------------------------------------


def _write_proc(proc_dir: Path, *, cgroup_path: str, mem_available_kib: int | None) -> None:
    (proc_dir / "self").mkdir(parents=True)
    (proc_dir / "self" / "cgroup").write_text(f"0::{cgroup_path}\n", encoding="utf-8")
    meminfo = "MemTotal:        8011708 kB\n"
    if mem_available_kib is not None:
        meminfo += f"MemAvailable:    {mem_available_kib} kB\n"
    (proc_dir / "meminfo").write_text(meminfo, encoding="utf-8")


def _write_cgroup(cgroup_dir: Path, *, memory_max: str, memory_current: int) -> None:
    cgroup_dir.mkdir(parents=True, exist_ok=True)
    (cgroup_dir / "memory.max").write_text(f"{memory_max}\n", encoding="utf-8")
    (cgroup_dir / "memory.current").write_text(f"{memory_current}\n", encoding="utf-8")


def test_unlimited_cgroup_reads_mem_available(tmp_path: Path) -> None:
    proc_dir, cgroup_mount = tmp_path / "proc", tmp_path / "cgroup"
    _write_proc(proc_dir, cgroup_path="/", mem_available_kib=4 * 1024 * 1024)
    _write_cgroup(cgroup_mount, memory_max="max", memory_current=3 * GIB)

    assert available_memory_bytes(proc_dir, cgroup_mount) == 4 * GIB


def test_cgroup_limit_headroom_is_used_when_below_mem_available(tmp_path: Path) -> None:
    proc_dir, cgroup_mount = tmp_path / "proc", tmp_path / "cgroup"
    _write_proc(proc_dir, cgroup_path="/", mem_available_kib=6 * 1024 * 1024)
    _write_cgroup(cgroup_mount, memory_max=str(4 * GIB), memory_current=1 * GIB)

    assert available_memory_bytes(proc_dir, cgroup_mount) == 3 * GIB


def test_mem_available_is_used_when_below_the_cgroup_limit_headroom(tmp_path: Path) -> None:
    proc_dir, cgroup_mount = tmp_path / "proc", tmp_path / "cgroup"
    _write_proc(proc_dir, cgroup_path="/", mem_available_kib=2 * 1024 * 1024)
    _write_cgroup(cgroup_mount, memory_max=str(16 * GIB), memory_current=1 * GIB)

    assert available_memory_bytes(proc_dir, cgroup_mount) == 2 * GIB


def test_cgroup_files_are_read_under_the_process_own_cgroup_path(tmp_path: Path) -> None:
    proc_dir, cgroup_mount = tmp_path / "proc", tmp_path / "cgroup"
    _write_proc(proc_dir, cgroup_path="/user.slice/session.scope", mem_available_kib=None)
    _write_cgroup(cgroup_mount, memory_max="max", memory_current=0)
    _write_cgroup(
        cgroup_mount / "user.slice" / "session.scope",
        memory_max=str(2 * GIB),
        memory_current=512 * MIB,
    )

    assert available_memory_bytes(proc_dir, cgroup_mount) == 2 * GIB - 512 * MIB


def test_cgroup_usage_above_its_limit_reads_as_no_headroom(tmp_path: Path) -> None:
    proc_dir, cgroup_mount = tmp_path / "proc", tmp_path / "cgroup"
    _write_proc(proc_dir, cgroup_path="/", mem_available_kib=None)
    _write_cgroup(cgroup_mount, memory_max=str(1 * GIB), memory_current=2 * GIB)

    assert available_memory_bytes(proc_dir, cgroup_mount) == 0


def test_no_readable_memory_source_reads_as_unknown(tmp_path: Path) -> None:
    assert available_memory_bytes(tmp_path / "proc", tmp_path / "cgroup") is None


# --- the hook, through a real pytest run ---------------------------------------


_PROJECT_TEST_FILE = """\
def test_one():
    pass


def test_two():
    pass
"""


@pytest.fixture(name="project")
def _project(  # pyright: ignore[reportUnusedFunction]  # pytest fixture, resolved by name
    tmp_path: Path,
) -> Path:
    (tmp_path / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    (tmp_path / "test_sample.py").write_text(_PROJECT_TEST_FILE, encoding="utf-8")
    return tmp_path


def _run_pytest(project: Path, args: Sequence[str]) -> str:
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in {"PYTEST_ADDOPTS", "PYTEST_XDIST_AUTO_NUM_WORKERS"}
    }
    completed = subprocess.run(  # noqa: S603 - fixed argv built from this module's own constants
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-p", PLUGIN_MODULE, *args],
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
        timeout=SUBPROCESS_TIMEOUT_SECONDS,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    return completed.stdout


def _created_workers(output: str) -> int:
    match = re.search(r"created: (\d+)/\1 workers?", output)
    assert match is not None, output
    return int(match.group(1))


def test_auto_runs_the_worker_count_its_announcement_states(project: Path) -> None:
    output = _run_pytest(project, ["-n", "auto"])

    announced = re.search(r"^xdist workers: (\d+) \(", output, flags=re.MULTILINE)
    assert announced is not None, output
    assert _created_workers(output) == int(announced.group(1))


def test_auto_announcement_shows_under_quiet_output(project: Path) -> None:
    output = _run_pytest(project, ["-q", "-n", "auto"])

    assert re.search(r"^xdist workers: \d+ \(", output, flags=re.MULTILINE), output


def test_explicit_worker_count_wins_and_is_not_announced(project: Path) -> None:
    output = _run_pytest(project, ["-n", "3"])

    assert _created_workers(output) == 3
    assert "xdist workers:" not in output
