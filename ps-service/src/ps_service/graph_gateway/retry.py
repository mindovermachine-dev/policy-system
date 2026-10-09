"""Bounded retry with backoff and failure classification for the gateway (issue #206, App-B).

Transient failures are retried on a fixed schedule (no jitter, so it is deterministic): a redis
`ConnectionError` (which includes `BusyLoadingError`), a redis `TimeoutError`, and
`GraphLogUnavailableError` for Postgres. When the budget runs out the caller sees a sanitized
error: `GraphUnavailableError` for the graph, the log's own `GraphLogUnavailableError` for
Postgres. Any other `redis.exceptions.RedisError` is permanent: it is not retried and becomes a
`GraphApplyError`. `GraphLogPersistenceError` is never caught here, since the outcome of a failed
commit is unknown and replaying it could log a group twice.

The sleep is injected; this module owns the one real default (`system_sleep`).
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

import redis.exceptions

from ps_service.dependency_health import FALKORDB, mark_unhealthy
from ps_service.graph_gateway.errors import (
    GraphApplyError,
    GraphLogUnavailableError,
    GraphUnavailableError,
)
from ps_service.graph_gateway.gateway_log import emit_gateway_event

if TYPE_CHECKING:
    from collections.abc import Callable

    from ps_service.graph_gateway.gateway import GatewaySettings
    from ps_service.logging import LogEmitter

TRANSIENT_ERRORS = (
    redis.exceptions.ConnectionError,
    redis.exceptions.TimeoutError,
    GraphLogUnavailableError,
)
"""The failures worth retrying: the dependency may answer again shortly."""


def system_sleep(seconds: float) -> None:
    """Wait `seconds` on the real clock (the production `sleep`)."""
    time.sleep(seconds)


def backoff_seconds(settings: GatewaySettings, attempt: int) -> float:
    """Return the wait after the `attempt`-th failed try (1-based): 0.2, 0.4, 0.8 by default."""
    return settings.initial_backoff_seconds * settings.backoff_multiplier ** (attempt - 1)


def call_with_backoff[T](
    operation: Callable[[], T],
    *,
    settings: GatewaySettings,
    sleep: Callable[[float], None],
    on_retry: Callable[[int, float, BaseException], None],
) -> T:
    """Call `operation`, retrying `TRANSIENT_ERRORS` up to `settings.max_attempts` tries.

    Returns its result. Raises the last transient error once the budget is spent, and any other
    error at once. `on_retry(attempt, delay, error)` runs before each wait.
    """
    attempt = 1
    while True:
        try:
            return operation()
        except TRANSIENT_ERRORS as exc:
            if attempt >= settings.max_attempts:
                raise
            delay = backoff_seconds(settings, attempt)
            on_retry(attempt, delay, exc)
            sleep(delay)
            attempt += 1


class GraphCallGuard:
    """Run one step against FalkorDB or the log with retries, raising sanitized errors only."""

    def __init__(
        self,
        settings: GatewaySettings,
        sleep: Callable[[float], None],
        emitter: LogEmitter | None,
    ) -> None:
        """Take the retry budget, the injected sleep and the log emitter."""
        self._settings = settings
        self._sleep = sleep
        self._emitter = emitter

    def call[T](
        self,
        graph: str,
        operation: Callable[[], T],
        *,
        position: Callable[[], int | None] = lambda: None,
    ) -> T:
        """Run `operation` for `graph`, retrying transient failures.

        `position()` names the first unapplied log position when a permanent failure is raised.

        Raises:
            GraphUnavailableError: FalkorDB stayed unreachable through the budget.
            GraphLogUnavailableError: Postgres stayed unreachable through the budget.
            GraphApplyError: FalkorDB refused the query; retrying cannot help.
        """

        def log_retry(attempt: int, delay: float, error: BaseException) -> None:
            self._log_retry(graph, attempt, delay, error)

        try:
            return call_with_backoff(
                operation, settings=self._settings, sleep=self._sleep, on_retry=log_retry
            )
        except GraphLogUnavailableError:
            raise
        except (redis.exceptions.ConnectionError, redis.exceptions.TimeoutError) as exc:
            unavailable = GraphUnavailableError()
            mark_unhealthy(FALKORDB, error=unavailable)
            raise unavailable from exc
        except redis.exceptions.RedisError as exc:
            raise GraphApplyError(graph, position()) from exc

    def _log_retry(self, graph: str, attempt: int, delay: float, error: BaseException) -> None:
        emit_gateway_event(
            "apply_retry",
            "retry",
            {
                "graph": graph,
                "attempt": attempt,
                "backoff_seconds": delay,
                "error_class": type(error).__name__,
            },
            emitter=self._emitter,
        )
