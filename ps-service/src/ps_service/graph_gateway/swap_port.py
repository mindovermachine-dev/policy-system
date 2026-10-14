"""The seam between the gateway and the graph store's key space, for swap sets.

A swap set is built on staged copies of the live graphs and the copies are renamed over the live
keys together. Copying and renaming are not graph queries, so they cannot go through the
`GraphHandle` the rest of the gateway uses: `GraphKeyPort` is the narrow Protocol for them. The
service composes the real port over its FalkorDB connection (`default_gateway`), the tests inject
an in-memory one. Only operations independently confirmed clean on the real engine are named here:
copy, delete and an atomic rename guarded by a watch.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Sequence
    from contextlib import AbstractContextManager


class KeySwapper(Protocol):
    """The one operation that needs the watch: rename staged keys over live ones, atomically."""

    def swap_in(self, pairs: Sequence[tuple[str, str]]) -> None:
        """Rename each `(staged, live)` pair in one atomic step, overwriting the live key.

        Args:
            pairs: `(staged key, live key)` pairs; every live key was named to `watching`.

        Raises:
            SwapConflictError: a watched live key changed since the watch began; nothing renamed.

        Side Effects:
            Overwrites each live key with its staged key on the server.
        """
        ...


class GraphKeyPort(Protocol):
    """Copy, drop and swap whole graph keys."""

    def copy(self, source: str, destination: str) -> None:
        """Copy graph `source` (schema and indexes included) to `destination`.

        An absent `source` copies as an empty graph, so a leg whose live graph was never created
        swaps in as a new one.

        Args:
            source: Key of the graph to copy.
            destination: Key to create.

        Side Effects:
            Creates the `destination` key on the server.
        """
        ...

    def discard(self, *names: str) -> None:
        """Delete every key in `names` that exists; a missing one is skipped, never an error.

        Args:
            names: Keys to delete; none deletes nothing.

        Side Effects:
            Deletes the keys on the server.
        """
        ...

    def watching(self, names: Sequence[str]) -> AbstractContextManager[KeySwapper]:
        """Watch the live keys `names` for the length of the block and offer the swap.

        The watch must begin before the copies are taken: a change to a watched key between the
        watch and the swap makes `swap_in` raise `SwapConflictError`. The block releases the
        watch however it ends.

        Args:
            names: Live graph keys to watch.

        Returns:
            A context manager yielding the `KeySwapper` for the block.

        Side Effects:
            Holds a watch on the keys for the length of the block.
        """
        ...
