"""Process-wide registry of each in-flight ingestion run's currently-executing stage.

Fed by :mod:`ps_service.api.ingestion_orchestration`'s pipeline sequencer
(``_execute_catalog_stages`` records the stage about to run;
``run_catalog_ingestion_pipeline`` clears the entry once the run ends, success
or failure) so a best-effort progress poller can read "what is this run doing
right now" without any queue, websocket, or external store (AC-BI-008).

Mirrors ``ps_service.dependency_health.registry``'s exact shape: a module-level
``dict`` guarded by a ``threading.Lock``, plain get/set/clear functions, and a
``reset_for_tests()`` for test isolation.

**No per-``run_id`` ownership check, by design.** This registry is a plain
``dict[str, str]`` keyed by ``run_id``, with no token or handle returned by
``set_stage`` that ``clear_stage`` must present back. If two concurrent runs
are ever given the *same* ``run_id``, their ``set_stage``/``clear_stage``
calls interleave on one key: whichever call lands last wins, and whichever
run finishes first clears the key out from under the other's still-in-flight
run. This is a deliberately accepted, best-effort tradeoff (see PLAN.md's D4
"Known limitation" note for issue #61), not a bug to fix here -- a colliding
``run_id`` degrades progress *display* only, it never changes a run's final
result, because this registry is read only by an optional progress poller,
never by the pipeline itself.
"""

from __future__ import annotations

import threading

_lock = threading.Lock()
_stage: dict[str, str] = {}  # run_id -> currently-executing stage name


def set_stage(run_id: str, stage: str) -> None:
    """Record that ``run_id`` is currently executing ``stage``.

    Overwrites any previously recorded stage for ``run_id``. Last-writer-wins
    if two runs ever share a ``run_id`` -- see the module docstring.
    """
    with _lock:
        _stage[run_id] = stage


def get_stage(run_id: str) -> str | None:
    """The stage currently recorded for ``run_id``, or ``None`` if unknown."""
    with _lock:
        return _stage.get(run_id)


def clear_stage(run_id: str) -> None:
    """Remove ``run_id``'s recorded stage, if any.

    A no-op if ``run_id`` has no recorded entry (e.g. already cleared, or
    never set).
    """
    with _lock:
        _stage.pop(run_id, None)


def reset_for_tests() -> None:
    """Clear all recorded state so each test starts with no runs in flight."""
    with _lock:
        _stage.clear()
