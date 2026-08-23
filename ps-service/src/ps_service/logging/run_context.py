"""Correlation-id binding/unbinding for a call chain — the BindRunContext
action (AC#1, AC#2). Reads/writes the `run_id` ContextVar only; no file I/O,
no queue — this module knows nothing about `emitter.py`.

Propagation boundary (G2, M5): a `contextvars` binding is visible to
everything deeper on the *same* thread/task's call stack, including a
`concurrent.futures.ThreadPoolExecutor` submission or an `asyncio.create_task`
(both fork/copy the caller's context at spawn time). It does NOT propagate to
a raw `threading.Thread` (each new OS thread starts with an empty context,
verified G2) — work handed to a raw thread must call `bind_run_context` again
inside that thread if it needs a run_id. A pooled worker that reuses a thread
across tasks relies on `bind_run_context`'s `finally` restore (below) to
avoid leaking one task's run_id into the next task scheduled on the same
worker.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager

import structlog.contextvars as ctxvars

_RUN_ID_KEY: str = "run_id"


@contextmanager
def bind_run_context(run_id: str | None = None) -> Iterator[str]:
    """Bind `run_id` (generating a uuid4 if omitted) for the current call chain.

    G1 fix (M4): structlog 26.1.0's `unbind_contextvars` DELETES the key from
    the current context rather than restoring an outer binding — a naive
    "unbind on exit" loses an outer `bind_run_context` call after a nested
    inner one exits. This implementation captures whatever run_id (if any)
    was already bound on entry and restores it — or unbinds cleanly if there
    was none — on exit (token-style capture/restore), so nested binds are
    correct: `with bind_run_context("A"): with bind_run_context("B"): ...`
    leaves `current_run_id() == "A"` after the inner block exits, and `None`
    after the outer block exits too.
    """
    active = run_id if run_id is not None else str(uuid.uuid4())
    prior = current_run_id()
    ctxvars.bind_contextvars(**{_RUN_ID_KEY: active})
    try:
        yield active
    finally:
        if prior is None:
            ctxvars.unbind_contextvars(_RUN_ID_KEY)
        else:
            ctxvars.bind_contextvars(**{_RUN_ID_KEY: prior})


def current_run_id() -> str | None:
    """Return the run_id bound in the *current* thread/task context, else None."""
    value = ctxvars.get_contextvars().get(_RUN_ID_KEY)
    return value if isinstance(value, str) else None
