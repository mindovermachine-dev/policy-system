"""Process-wide registry of each in-flight ingestion run's currently-executing stage.

Purpose: let a best-effort progress poller answer "what is this run doing right now"
without a queue, websocket or external store. The ingestion pipeline sequencer records the
stage about to run and clears the entry when the run ends, whether it succeeded or failed.

The registry is a module-level ``dict[str, str]`` keyed by ``run_id`` and guarded by a
``threading.Lock``; it has plain get/set/clear functions and ``reset_for_tests()`` for test
isolation.

Business Rules:
    There is no per-``run_id`` ownership check. ``set_stage`` returns no token that
    ``clear_stage`` must present. If two concurrent runs ever share a ``run_id``, their calls
    interleave on one key: the last call wins, and whichever run finishes first clears the key
    while the other is still running. This best-effort trade-off only degrades the progress
    display. It never changes a run's result, because only an optional progress poller reads
    the registry and the pipeline never does.
"""

from __future__ import annotations

import threading

_lock = threading.Lock()
_stage: dict[str, str] = {}  # run_id -> currently-executing stage name


def set_stage(run_id: str, stage: str) -> None:
    """Record that a run is currently executing a pipeline stage.

    Args:
        run_id: Identifier of the ingestion run; any string, used as the registry key.
        stage: Name of the stage the run is about to execute (for example ``"ingestion"``).
            Not validated.

    Returns:
        None.

    Side Effects:
        Overwrites any stage already recorded for ``run_id`` in the process-wide registry. If
        two runs share a ``run_id`` the last writer wins.
    """
    with _lock:
        _stage[run_id] = stage


def get_stage(run_id: str) -> str | None:
    """Read the stage currently recorded for a run.

    Args:
        run_id: Identifier of the ingestion run to look up.

    Returns:
        The stage name last recorded for ``run_id``, or ``None`` if no stage is recorded (the
        run never started, has finished, or was cleared).
    """
    with _lock:
        return _stage.get(run_id)


def clear_stage(run_id: str) -> None:
    """Remove the stage recorded for a run.

    Args:
        run_id: Identifier of the ingestion run whose entry is removed.

    Returns:
        None. Clearing a ``run_id`` with no recorded entry (already cleared or never set) is
        a no-op, not an error.

    Side Effects:
        Deletes the entry for ``run_id`` from the process-wide registry.
    """
    with _lock:
        _stage.pop(run_id, None)


def reset_for_tests() -> None:
    """Clear every recorded stage so a test starts with no runs in flight.

    Returns:
        None.

    Side Effects:
        Empties the process-wide registry for all run ids.
    """
    with _lock:
        _stage.clear()
