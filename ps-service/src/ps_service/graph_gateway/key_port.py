# © 2026 Cartman ApS. All rights reserved.
"""Raw FalkorDB/Redis connection primitives for the staged-write swap.

This package is the only one allowed to touch the driver. `falkordb.FalkorDB(...).connection` is a
real `redis.Redis(...)` client, so every Redis command (`RENAME`/`DELETE`/`pipeline()`) is
reachable directly. `redis` ships `py.typed`; once a value is narrowed to this module's Protocols
no further per-call suppression is needed.

Three narrow structural Protocols, each with one conversion site, keep the driver out of callers:

- `_RawGraphConnection`: the raw, single-key Redis-native primitives (`rename`/`delete`/
  `pipeline`) the gateway's swap port needs. `pipeline()` returns `_WatchablePipeline` so a
  caller can WATCH/MULTI/EXEC. `dump`/`restore` are deliberately absent: FalkorDB corrupts data
  across that command pair.
- `_GraphCopyHandle`: the `GRAPH.COPY` and Cypher `query()` surface the gateway's swap port
  needs (`falkordb.Graph.copy` and `falkordb.Graph.query`).
- `_GraphQueryHandle`: the bare parameterized-Cypher surface `serialize_graph` needs, nothing
  else. Distinct from `_GraphCopyHandle` (which adds `.copy()`) even though a real
  `falkordb.Graph` satisfies both: each Protocol is scoped to what its own caller uses.

The Protocols are private by naming convention, but `_RawGraphConnection` and `_GraphCopyHandle`
are imported by the gateway's swap port.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, cast

if TYPE_CHECKING:
    from falkordb import FalkorDB


class _WatchablePipeline(Protocol):
    """Structural stand-in for `redis.client.Pipeline`'s WATCH/MULTI/EXEC surface.

    Only a `Pipeline` (the return value of `connection.pipeline(transaction=True)`) honours
    `WATCH`; a bare `redis.Redis` client's own `watch()` is a deprecated no-op that never issues
    `WATCH` to the server. A caller must hold a single `Pipeline` object across `watch()`, a read
    of the watched key, `multi()`, the queued commands and `execute()` for `WATCH` to have any
    effect; `reset()` must always run afterward (in a `finally`) to release the pipeline's held
    connection and any outstanding watches.
    """

    def watch(self, *names: str) -> None:
        """Mark key names as watched.

        Args:
            *names: Redis key names; a later `execute()` aborts if any of them changed.
        """
        ...

    def multi(self) -> None:
        """Switch a watched pipeline back into command-queuing mode.

        Side Effects:
            Subsequent commands are queued, not run, until `execute()`.
        """
        ...

    def rename(self, src: str, dst: str) -> object:  # noqa: V107 - protocol parameter names the call contract
        """Queue a `RENAME` (Redis's native rename that overwrites the destination).

        Args:
            src: Existing key name.
            dst: New key name; an existing key of that name is overwritten.

        Returns:
            The pipeline's queuing acknowledgement; its value is not used.
        """
        ...

    def execute(self) -> list[object]:
        """Run every queued command atomically.

        Returns:
            One reply per queued command, in queue order.

        Raises:
            redis.exceptions.WatchError: a watched key changed since `watch()` was called.
        """
        ...

    def reset(self) -> None:
        """Release the pipeline's held connection and any watches. Always call in `finally`.

        Side Effects:
            Returns the connection to the pool and drops outstanding `WATCH`es.
        """
        ...


class _RawGraphConnection(Protocol):
    """Structural stand-in for `falkordb.FalkorDB(...).connection` (a real `redis.Redis`).

    Narrows the full `redis.Redis` surface to exactly the raw, single-key primitives the
    staged-write sequence needs: `RENAME`/`DELETE`, plus `pipeline()` for the atomic multi-`RENAME`
    finalize step. `dump`/`restore` are left out because FalkorDB corrupts data across that
    command pair. Callers outside this module never import `redis.Redis` or
    `falkordb.FalkorDB.connection` directly; `raw_connection` is the one conversion site.
    """

    def rename(self, src: str, dst: str) -> bool:  # noqa: V107 - protocol parameter names the call contract
        """Atomically rename a key, silently overwriting an existing destination.

        Args:
            src: Existing key name.
            dst: New key name.

        Returns:
            True when the rename succeeded.
        """
        ...

    def delete(self, *names: str) -> int:
        """Delete zero or more keys. A missing name is silently skipped, never an error.

        Args:
            *names: Key names to delete.

        Returns:
            The number of keys actually deleted.
        """
        ...

    def pipeline(self, *, transaction: bool = True) -> _WatchablePipeline:
        """Open a pipeline that queues commands for one atomic `MULTI`/`EXEC` block.

        Args:
            transaction: Whether the queued commands run as one `MULTI`/`EXEC` block; keep True
                to get atomicity and `WATCH` support.

        Returns:
            A pipeline the caller must `reset()` when done.
        """
        ...


class _GraphCopyHandle(Protocol):
    """Structural stand-in for `falkordb.Graph`'s copy/query surface used by staging.

    Narrows to exactly what the gateway's swap port needs: `GRAPH.COPY` and a plain Cypher
    `query()` for the fallback that creates an empty graph key. Callers never import
    `falkordb.Graph` directly for this purpose; `graph_copy_handle` is the one conversion site.
    """

    def copy(self, clone: str) -> object:  # noqa: V107 - protocol parameter names the call contract
        """Clone this graph (schema included) under a new name (`GRAPH.COPY`).

        Args:
            clone: Name of the new graph.

        Returns:
            The driver's reply; its value is not used.

        Side Effects:
            Creates the graph `clone` in FalkorDB.
        """
        ...

    def query(self, q: str, params: dict[str, object] | None = None) -> object:
        """Run Cypher against this graph.

        Args:
            q: The Cypher text.
            params: Query parameters; None for none.

        Returns:
            The driver's query result, whose rows the caller narrows on use.
        """
        ...


def raw_connection(db: FalkorDB) -> _RawGraphConnection:
    """Expose a FalkorDB handle's underlying `redis.Redis` client as `_RawGraphConnection`.

    The one conversion site for this Protocol: `db.connection` is already a real
    `redis.Redis(...)` instance, returned unchanged, with no wrapping and no copy.

    Args:
        db: A connected FalkorDB handle.

    Returns:
        The handle's own Redis client, typed as the narrow Protocol.
    """
    # cast: redis-py declares RENAME/DELETE/pipeline via overloaded
    # `self: SyncClientProtocol` / `self: AsyncClientProtocol` self-types (its own
    # sync/async dispatch mechanism), which basedpyright cannot structurally match
    # against a plain external Protocol even though the real methods' call shapes
    # conform. This is the one unavoidable cast, at the single conversion site.
    return cast("_RawGraphConnection", db.connection)


def graph_copy_handle(db: FalkorDB, name: str) -> _GraphCopyHandle:
    """Select a graph on a FalkorDB handle, typed as `_GraphCopyHandle`.

    The one conversion site for this Protocol.

    Args:
        db: A connected FalkorDB handle.
        name: Graph name; selecting does not create the graph until it is written.

    Returns:
        The selected graph, typed as the narrow copy/query Protocol.
    """
    return db.select_graph(name)


class _GraphQueryHandle(Protocol):
    """Structural stand-in for `falkordb.Graph`'s bare parameterized-Cypher surface.

    Exactly what `export.serialize.serialize_graph` needs, nothing else. Distinct from
    `_GraphCopyHandle` (adds `.copy()`, used only by the swap port) even though a real
    `falkordb.Graph` satisfies both: each Protocol is scoped to what its own caller uses.
    """

    def query(self, q: str, params: dict[str, object] | None = None) -> object:
        """Run Cypher against this graph.

        Args:
            q: The Cypher text.
            params: Query parameters; None for none.

        Returns:
            The driver's query result, whose rows the caller narrows on use.
        """
        ...


def graph_query_handle(db: FalkorDB, name: str) -> _GraphQueryHandle:
    """Select a graph on a FalkorDB handle, typed as `_GraphQueryHandle`. One conversion site.

    Args:
        db: A connected FalkorDB handle.
        name: Graph name; selecting does not create the graph until it is written.

    Returns:
        The selected graph, typed as the bare Cypher-query Protocol.
    """
    return db.select_graph(name)
