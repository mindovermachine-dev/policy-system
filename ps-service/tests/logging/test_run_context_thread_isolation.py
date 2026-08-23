"""Tests for run_id propagation across real threads (AC#2, M5)."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

from ps_service.logging import bind_run_context, current_run_id


def test_run_id_when_two_threads_bind_concurrently_then_ids_stay_disjoint() -> None:
    observed: dict[str, str | None] = {}

    def worker(tag: str) -> None:
        with bind_run_context(tag):
            observed[tag] = current_run_id()

    threads = [threading.Thread(target=worker, args=(tag,)) for tag in ("thread-a", "thread-b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert observed == {"thread-a": "thread-a", "thread-b": "thread-b"}


def test_run_id_when_raw_thread_spawned_without_binding_then_sees_no_run_id() -> None:
    observed: dict[str, str | None] = {}

    def worker() -> None:
        observed["seen"] = current_run_id()

    with bind_run_context("outer"):
        t = threading.Thread(target=worker)
        t.start()
        t.join()

    # M5 boundary: a raw thread does not inherit the parent's contextvars binding
    assert observed["seen"] is None


def test_run_id_when_thread_pool_worker_reused_then_no_leak_between_tasks() -> None:
    def task(tag: str) -> str | None:
        with bind_run_context(tag):
            return current_run_id()

    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(task, "task-1").result()
        second = pool.submit(task, "task-2").result()

    assert first == "task-1"
    assert second == "task-2"
