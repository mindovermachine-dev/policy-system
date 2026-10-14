"""Retry of the startup replay through an infrastructure outage.

A startup replay that meets Postgres or FalkorDB down cannot finish, and the service must not
take traffic until it has. Giving up would leave the pod at `not_ready` for good, so the replay is
tried again with the shared backoff schedule (`retry.backoff_seconds`) capped at
`GatewaySettings.startup_replay_max_backoff_seconds`, without a limit on the number of tries. Each
retry resumes from the graphs' progress records. The wait returns True when a stop was requested,
and the retrying ends there with the last outage error. Anything that is not an outage (a refused
query, a bug) is not retried: waiting cannot fix it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ps_service.graph_gateway.retry import TRANSIENT_ERRORS, backoff_seconds

if TYPE_CHECKING:
    from collections.abc import Callable

    from ps_service.graph_gateway.gateway import GatewaySettings


def retry_through_outage[T](
    operation: Callable[[], T],
    *,
    settings: GatewaySettings,
    wait: Callable[[float], bool],
    on_retry: Callable[[int, float, BaseException], None],
) -> T:
    """Call `operation` until it returns, retrying `TRANSIENT_ERRORS` with a capped growing wait.

    Args:
        operation: The step to run; called again after each outage failure.
        settings: Supplies the backoff schedule and `startup_replay_max_backoff_seconds`.
        wait: `wait(delay)` sleeps `delay` seconds and returns True when a stop was requested.
        on_retry: Called as `on_retry(attempt, delay, error)` before each wait.

    Returns:
        The result of the first successful call to `operation`.

    Raises:
        Exception: The last outage error once `wait` reports a stop, and any other error from
            `operation` at once.

    Side Effects:
        Calls `wait` and `on_retry` between tries; the number of tries is unbounded.
    """
    attempt = 1
    while True:
        try:
            return operation()
        except TRANSIENT_ERRORS as exc:
            delay = min(
                backoff_seconds(settings, attempt), settings.startup_replay_max_backoff_seconds
            )
            on_retry(attempt, delay, exc)
            if wait(delay):
                raise
            attempt += 1
