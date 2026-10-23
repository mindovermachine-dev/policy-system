"""The process slot that lets ingestion code record a run outcome without holding a handle.

`build_metrics` installs its ingestion instruments here and `MetricsHandle.shutdown`
uninstalls them. The slot is last-writer-wins: production runs one app per process, and a
test that builds several apps records into the newest. With nothing installed, recording is
a no-op, so code paths that run without `create_app` are unaffected.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from opentelemetry.metrics import Counter, Histogram

type IngestionOutcome = Literal["succeeded", "failed"]


@dataclass(frozen=True)
class IngestionRecorder:
    """The ingestion run instruments of one app.

    Attributes:
        runs: Counter of completed runs, labelled by outcome.
        duration: Histogram of run duration in seconds, labelled by outcome.
    """

    runs: Counter
    duration: Histogram


_lock = threading.Lock()
_installed: IngestionRecorder | None = None


def install(recorder: IngestionRecorder) -> None:
    """Make `recorder` the process's ingestion recorder, replacing any earlier one."""
    global _installed  # noqa: PLW0603 -- the one process slot, see the module docstring
    with _lock:
        _installed = recorder


def uninstall(recorder: IngestionRecorder) -> None:
    """Clear the slot only if it holds `recorder`, so an older app cannot unhook a newer one."""
    global _installed  # noqa: PLW0603 -- the one process slot, see the module docstring
    with _lock:
        if _installed is recorder:
            _installed = None


def reset_for_tests() -> None:
    """Empty the slot so one test's handle never records another test's runs."""
    global _installed  # noqa: PLW0603 -- the one process slot, see the module docstring
    with _lock:
        _installed = None


def record_ingestion_run(outcome: IngestionOutcome, duration_seconds: float) -> None:
    """Count one finished ingestion run and observe its duration.

    Call it only for the write that won the terminal compare-and-swap, so each run is counted
    once and a lost race or a failed write counts nothing.

    Args:
        outcome: `succeeded` or `failed`; the only label recorded.
        duration_seconds: Run duration; a negative value is recorded as zero.

    Side Effects:
        Updates the installed counter and histogram; does nothing when none is installed.
    """
    with _lock:
        recorder = _installed
    if recorder is None:
        return
    attributes = {"outcome": outcome}
    recorder.runs.add(1, attributes)
    recorder.duration.record(max(duration_seconds, 0.0), attributes)
