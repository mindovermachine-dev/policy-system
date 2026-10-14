"""Build a swap set on staged copies of its graphs and flip them in together.

The caller holds the in-process locks of every leg. For each leg this copies the live graph to a
staged key named after the set, applies the leg's logged entries beyond its applied marker to the
staged key with the same applier the live path and the replay use (so a swap and a replay cannot
disagree), then renames all staged keys over the live keys in one atomic step. Nothing here
touches a live key before that rename and nothing moves an applied marker: the caller advances the
markers once the rename has happened. A rename the watch refuses restarts from a fresh copy, up to
`GatewaySettings.max_attempts` tries.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

import redis.exceptions

from ps_service.graph_gateway.applier import apply_entries
from ps_service.graph_gateway.errors import (
    GraphApplyError,
    GraphUnavailableError,
    SwapConflictError,
)
from ps_service.graph_gateway.gateway_log import emit_gateway_event

if TYPE_CHECKING:
    import uuid
    from collections.abc import Callable, Sequence

    from ps_service.graph_gateway.connection import WritableGraph
    from ps_service.graph_gateway.gateway import GatewaySettings
    from ps_service.graph_gateway.models import GraphLogEntry, SwapGroupRef
    from ps_service.graph_gateway.retry import GraphCallGuard
    from ps_service.graph_gateway.store import GraphLogStore
    from ps_service.graph_gateway.swap_port import GraphKeyPort, KeySwapper
    from ps_service.logging import LogEmitter

STAGED_KEY_MARKER = "__swap__"
"""Between a graph's name and the swap set's id in the name of its staged key."""


def staged_name(graph: str, swap_set_id: uuid.UUID) -> str:
    """Name the staged key of `graph` for one swap set (unique to the set).

    Args:
        graph: Name of the live graph.
        swap_set_id: Id of the swap set.

    Returns:
        `{graph}__swap__{swap_set_id}`.
    """
    return f"{graph}{STAGED_KEY_MARKER}{swap_set_id}"


def _ignore(position: int) -> None:
    """Ignore a run callback: a staged apply moves no marker (the caller moves them later)."""
    del position


class SwapApplier:
    """Stages, applies and flips the legs of one swap set; the caller holds every leg's lock."""

    def __init__(
        self,
        *,
        log_store: GraphLogStore,
        graph_opener: Callable[[str], WritableGraph],
        key_port: GraphKeyPort,
        settings: GatewaySettings,
        guard: GraphCallGuard,
        emitter: LogEmitter | None = None,
    ) -> None:
        """Take the log, the graph opener, the key port, the retry budget and the call guard.

        Args:
            log_store: Source of pending log entries and applied markers.
            graph_opener: Opens a writable handle on a named graph key.
            key_port: Copies, discards and swaps whole graph keys.
            settings: Supplies `max_attempts` and the apply batch size.
            guard: Runs each step with retries and sanitized errors.
            emitter: Destination of the `swap_discard` entries; `None` disables logging.
        """
        self._log_store = log_store
        self._graph_opener = graph_opener
        self._key_port = key_port
        self._settings = settings
        self._guard = guard
        self._emitter = emitter

    def apply(self, swap_set_id: uuid.UUID, legs: Sequence[SwapGroupRef]) -> float:
        """Make every leg's live graph hold its staged result; return the seconds that took.

        Every staged key is gone when this returns or raises, except when the process itself dies.

        Args:
            swap_set_id: Id of the swap set; names the staged keys.
            legs: The set's pending legs in ascending graph order; at least one.

        Returns:
            The seconds the successful try took, from the first copy to the rename.

        Raises:
            SwapConflictError: the watch refused the rename on every try.
            GraphUnavailableError: the graph store stayed unreachable through the retry budget.
            GraphLogUnavailableError: the log stayed unreachable through the retry budget.
            GraphApplyError: the graph store refused a copy or an entry.

        Side Effects:
            Creates and deletes staged graph keys, replaces each live key by its staged copy and
            emits a `swap_discard` entry when cleanup fails. It moves no applied marker.
        """
        names = {leg.graph: staged_name(leg.graph, swap_set_id) for leg in legs}
        pending = {leg.graph: self._pending_entries(leg.graph) for leg in legs}
        started = time.monotonic()
        for attempt in range(1, self._settings.max_attempts + 1):
            try:
                self._attempt(legs, names, pending)
            except SwapConflictError:
                self._discard(names)
                if attempt == self._settings.max_attempts:
                    raise
            except Exception:
                self._discard(names)
                raise
            else:
                return time.monotonic() - started
        raise AssertionError  # pragma: no cover  (max_attempts is at least 1)

    def _pending_entries(self, graph: str) -> tuple[GraphLogEntry, ...]:
        """Read the entries of `graph` beyond its applied marker."""
        return self._guard.call(
            graph,
            lambda: self._log_store.read_entries(
                graph, after_position=self._log_store.read_applied_position(graph)
            ),
        )

    def _attempt(
        self,
        legs: Sequence[SwapGroupRef],
        names: dict[str, str],
        pending: dict[str, tuple[GraphLogEntry, ...]],
    ) -> None:
        """One try: fresh staged copies with the entries applied, then the atomic rename."""
        self._discard(names)  # a staged key a dead process left behind never survives into a try
        with self._key_port.watching([leg.graph for leg in legs]) as swapper:
            for leg in legs:
                self._stage_leg(leg.graph, names[leg.graph], pending[leg.graph])
            self._flip(swapper, [(names[leg.graph], leg.graph) for leg in legs], legs[0].graph)

    def _stage_leg(self, graph: str, staged: str, entries: tuple[GraphLogEntry, ...]) -> None:
        """Copy `graph` to `staged` and apply `entries` to the copy."""
        self._guard.call(graph, lambda: self._key_port.copy(graph, staged))
        first = entries[0].position if entries else None
        self._guard.call(
            graph,
            lambda: apply_entries(
                self._graph_opener(staged),
                entries,
                batch_size=self._settings.batch_size,
                on_run_applied=_ignore,
            ),
            position=lambda: first,
        )

    def _flip(self, swapper: KeySwapper, pairs: list[tuple[str, str]], graph: str) -> None:
        """Rename the staged keys over the live ones once, never retrying an unknown outcome.

        A connection that fails during the rename may or may not have renamed: asking again could
        fail on a key that is gone, so the failure goes up and recovery redoes the set from a copy
        of whatever the live graphs now hold (the entries are idempotent).
        """
        try:
            swapper.swap_in(pairs)
        except (redis.exceptions.ConnectionError, redis.exceptions.TimeoutError) as exc:
            raise GraphUnavailableError from exc
        except redis.exceptions.RedisError as exc:
            raise GraphApplyError(graph) from exc

    def _discard(self, names: dict[str, str]) -> None:
        """Drop the staged keys; an unreachable graph store leaves them to the next try."""
        try:
            self._key_port.discard(*names.values())
        except redis.exceptions.RedisError as exc:
            emit_gateway_event(
                "swap_discard",
                "failure",
                {"error_class": type(exc).__name__},
                emitter=self._emitter,
            )
