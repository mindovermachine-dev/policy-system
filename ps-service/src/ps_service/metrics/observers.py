"""Observable-gauge callbacks that can never fail a scrape.

Every callback is wrapped by `guarded`: an exception from a metric source is logged once as a
semantic entry (exception class only) and the gauge is omitted from that scrape.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING

from opentelemetry.metrics import Observation

from ps_service.logging import LoggingLifecycleError, emit_log_entry

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    from opentelemetry.metrics import CallbackOptions

    from ps_service.metrics.instruments import Observer

_COMPONENT = "metrics"


def log_source_failure(source: str, exc: Exception) -> None:
    """Log one `failed` entry for a metric source; never raise.

    Args:
        source: Short source name, used as the `collect_<source>` action.
        exc: The exception; only its class name is recorded, never its message.

    Side Effects:
        Enqueues one log entry; a process with no configured emitter is tolerated.
    """
    with contextlib.suppress(LoggingLifecycleError):
        emit_log_entry(
            component=_COMPONENT,
            action=f"collect_{source}",
            outcome="failed",
            extra={"error_type": type(exc).__name__},
        )


def guarded(source: str, produce: Callable[[], Iterable[Observation]]) -> Observer:
    """Wrap `produce` as an observable-gauge callback that omits the gauge when it raises.

    Args:
        source: Short source name for the failure log entry.
        produce: Returns the observations for one scrape.

    Returns:
        A callback that returns `produce()`'s observations, or none after logging a failure.
    """

    def observe(_options: CallbackOptions) -> Iterable[Observation]:
        try:
            return list(produce())
        except Exception as exc:  # noqa: BLE001 -- a metric source must never fail a scrape
            log_source_failure(source, exc)
            return []

    return observe


def dependency_observations(
    counts: Callable[[], tuple[int, int]],
) -> Callable[[], list[Observation]]:
    """Build the two-series observation producer for the unhealthy-dependency gauge.

    Args:
        counts: Returns `(gating_unhealthy, non_gating_unhealthy)`.

    Returns:
        A producer yielding one observation per `gating` label value.
    """

    def produce() -> list[Observation]:
        gating, other = counts()
        return [Observation(gating, {"gating": "true"}), Observation(other, {"gating": "false"})]

    return produce


def pending_observations(total: Callable[[], int | None]) -> Callable[[], list[Observation]]:
    """Build the producer for the pending graph-log gauge.

    Args:
        total: Returns the pending-entry total, or `None` while the source has no value.

    Returns:
        A producer yielding one unlabelled observation, or none when there is no value.
    """

    def produce() -> list[Observation]:
        value = total()
        return [] if value is None else [Observation(max(value, 0))]

    return produce
