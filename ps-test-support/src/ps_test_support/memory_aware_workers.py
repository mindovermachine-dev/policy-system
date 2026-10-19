"""Pytest plugin: size xdist's `-n auto` by available memory as well as CPUs.

`addopts` in the root `pyproject.toml` runs every invocation under `pytest-xdist` with
`-n auto`, which counts CPUs only. Each worker holds several hundred MiB, so on a host with
many CPUs and little memory the kernel OOM-kills workers mid-run. This plugin, loaded with
`-p ps_test_support.memory_aware_workers` from `addopts`, caps the `auto` (and `logical`)
worker count at the number of per-worker memory budgets that fit in the available memory.

It is a `-p` plugin rather than a `conftest.py` so that it applies to every run from the
repository root, whichever test paths are given.

An explicit `-n N` and the `PYTEST_XDIST_AUTO_NUM_WORKERS` environment variable always win:
xdist only asks for an automatic count when neither is set to a number.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING, Final

import pytest

if TYPE_CHECKING:
    from collections.abc import Generator

_MIB: Final = 1024 * 1024

PER_WORKER_MEMORY_BUDGET_BYTES: Final = 800 * _MIB
"""Memory reserved per xdist worker: the largest worker RSS sampled in a full run is 781 MiB."""

_AUTO_NUM_WORKERS_ENV: Final = "PYTEST_XDIST_AUTO_NUM_WORKERS"
_DEFAULT_PROC_DIR: Final = Path("/proc")
_DEFAULT_CGROUP_MOUNT: Final = Path("/sys/fs/cgroup")
_ANNOUNCEMENT_KEY: Final = pytest.StashKey[str]()


def worker_count(
    cpu_workers: int,
    available_bytes: int | None,
    per_worker_bytes: int = PER_WORKER_MEMORY_BUDGET_BYTES,
) -> int:
    """Return how many xdist workers fit in both the CPUs and the available memory.

    The CPU count is an upper bound; the available memory allows one worker per whole
    `per_worker_bytes` budget. At least one worker always runs. When the available memory
    is unknown, the CPU count is used unchanged. Reads and writes nothing.

    Args:
        cpu_workers: The worker count xdist chose from the CPUs, 1 or more.
        available_bytes: Memory free for new processes, or `None` when it could not be read.
        per_worker_bytes: Memory reserved per worker, greater than 0.

    Returns:
        `max(1, min(cpu_workers, available_bytes // per_worker_bytes))`, or `cpu_workers`
        when `available_bytes` is `None`.

    Example:
        >>> worker_count(8, 4 * 1024**3, per_worker_bytes=800 * 1024**2)
        5
    """
    if available_bytes is None:
        return cpu_workers
    return max(1, min(cpu_workers, available_bytes // per_worker_bytes))


def _read_int(path: Path) -> int | None:
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except OSError, ValueError:
        return None


def _own_cgroup_dir(proc_dir: Path, cgroup_mount: Path) -> Path:
    """The cgroup v2 directory of this process, or the mount root when it cannot be read."""
    try:
        lines = (proc_dir / "self" / "cgroup").read_text(encoding="utf-8").splitlines()
    except OSError:
        return cgroup_mount
    for line in lines:
        if line.startswith("0::"):
            return cgroup_mount / line.removeprefix("0::").lstrip("/")
    return cgroup_mount


def _cgroup_headroom_bytes(cgroup_dir: Path) -> int | None:
    """`memory.max - memory.current`, or `None` when no cgroup v2 limit is set or readable."""
    limit = _read_int(cgroup_dir / "memory.max")
    current = _read_int(cgroup_dir / "memory.current")
    if limit is None or current is None:
        return None
    return max(0, limit - current)


def _mem_available_bytes(proc_dir: Path) -> int | None:
    """`MemAvailable` from `meminfo`, or `None` when it is missing or unreadable."""
    try:
        lines = (proc_dir / "meminfo").read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in lines:
        name, _, value = line.partition(":")
        if name == "MemAvailable":
            kib = value.split()
            return int(kib[0]) * 1024 if kib and kib[0].isdigit() else None
    return None


def available_memory_bytes(
    proc_dir: Path = _DEFAULT_PROC_DIR,
    cgroup_mount: Path = _DEFAULT_CGROUP_MOUNT,
) -> int | None:
    """Return the memory new processes can use before the kernel OOM-kills one.

    Two Linux sources are read: the headroom under this process's cgroup v2 memory limit
    (`memory.max` minus `memory.current`, only when `memory.max` is a number) and
    `MemAvailable` from `meminfo`. The smaller one wins, because either limit alone can
    trigger the OOM killer. On a system with neither (non-Linux), the result is unknown.

    Args:
        proc_dir: The procfs mount, read for `self/cgroup` and `meminfo`.
        cgroup_mount: The cgroup v2 mount, read for `memory.max` and `memory.current`.

    Returns:
        Available memory in bytes, or `None` when no source could be read.

    Side Effects:
        Reads files under `proc_dir` and `cgroup_mount`; writes nothing.
    """
    sources = (
        _cgroup_headroom_bytes(_own_cgroup_dir(proc_dir, cgroup_mount)),
        _mem_available_bytes(proc_dir),
    )
    readable = [value for value in sources if value is not None]
    return min(readable) if readable else None


def _announcement(workers: int, cpu_workers: int, available_bytes: int | None) -> str:
    budget_mib = PER_WORKER_MEMORY_BUDGET_BYTES // _MIB
    if available_bytes is None:
        return f"xdist workers: {workers} (available memory unreadable; CPU count {cpu_workers})"
    memory_workers = available_bytes // PER_WORKER_MEMORY_BUDGET_BYTES
    limit = "memory" if memory_workers < cpu_workers else "CPU count"
    return (
        f"xdist workers: {workers} ({limit}-bound; CPU count {cpu_workers}, "
        f"{available_bytes // _MIB} MiB available / {budget_mib} MiB per worker)"
    )


@pytest.hookimpl(wrapper=True)
def pytest_xdist_auto_num_workers(config: pytest.Config) -> Generator[None, int, int]:
    """Cap xdist's CPU-based `-n auto` worker count by the available memory.

    Purpose: `-n auto` must not start more workers than the host's memory holds, or the
    kernel OOM-kills them mid-run.

    Args:
        config: The pytest configuration; the chosen count's announcement is stashed on it.

    Returns:
        The capped worker count. xdist's own count is returned unchanged when
        `PYTEST_XDIST_AUTO_NUM_WORKERS` is set.

    Side Effects:
        Reads the Linux memory sources (see `available_memory_bytes`) and stashes the
        announcement line that `pytest_sessionstart` prints.
    """
    cpu_workers = yield
    if os.environ.get(_AUTO_NUM_WORKERS_ENV):
        return cpu_workers
    available = available_memory_bytes()
    workers = worker_count(cpu_workers, available)
    config.stash[_ANNOUNCEMENT_KEY] = _announcement(workers, cpu_workers, available)
    return workers


def pytest_sessionstart(session: pytest.Session) -> None:
    """Print the automatic worker count and its reason, at every verbosity.

    Args:
        session: The pytest session; its config carries the stashed announcement.

    Side Effects:
        Writes one line to the terminal when `-n auto` chose the worker count.
    """
    announcement = session.config.stash.get(_ANNOUNCEMENT_KEY, None)
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    if announcement is not None and reporter is not None:
        reporter.write_line(announcement)
