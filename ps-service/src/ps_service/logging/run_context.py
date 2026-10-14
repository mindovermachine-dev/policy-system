"""Run-id (correlation id) binding for a call chain.

Purpose: let log entries emitted anywhere in a call chain carry the same `run_id`. The module
reads and writes only the `run_id` context variable; it does no file I/O and knows nothing
about the emitter.

Propagation boundary: a binding is visible to everything deeper on the same thread or task,
including work submitted to a `concurrent.futures.ThreadPoolExecutor` or started with
`asyncio.create_task` (both copy the caller's context at spawn time). It does not propagate to
a raw `threading.Thread`, whose context starts empty; work handed to a raw thread must call
`bind_run_context` again inside that thread if it needs a run id. A pooled worker that reuses
a thread across tasks relies on `bind_run_context` restoring the previous value on exit, so
one task's run id does not leak into the next.
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from typing import TYPE_CHECKING

import structlog.contextvars as ctxvars

if TYPE_CHECKING:
    from collections.abc import Generator

_RUN_ID_KEY: str = "run_id"


@contextmanager
def bind_run_context(run_id: str | None = None) -> Generator[str]:
    """Bind a run id for the current call chain for the duration of a `with` block.

    On entry the run id already bound (if any) is captured; on exit it is restored, or the
    binding is removed cleanly when there was none. Nested binds are therefore correct:
    after `with bind_run_context("A"): with bind_run_context("B"): ...` the inner exit leaves
    `current_run_id() == "A"`, and the outer exit leaves `None`. Restoration happens even when
    the block raises.

    Args:
        run_id: The correlation id to bind. None generates a random UUID4 string. An empty
            string is bound as given.

    Yields:
        The run id bound inside the block: `run_id` when given, otherwise the generated one.

    Side Effects:
        Sets the `run_id` context variable for the current thread or task, and restores the
        prior value on exit.

    Example:
        >>> with bind_run_context("run-1") as active:
        ...     current_run_id() == active == "run-1"
        True
        >>> current_run_id() is None
        True
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
    """Return the run id bound in the current thread or task context.

    Returns:
        The bound run id, or None when none is bound (or the bound value is not a string).
    """
    value = ctxvars.get_contextvars().get(_RUN_ID_KEY)
    return value if isinstance(value, str) else None
