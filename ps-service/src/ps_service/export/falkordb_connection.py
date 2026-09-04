"""Raw FalkorDB/Redis connection primitives for D8's staged-write sequence.

PLAN.md §0.4 confirms `falkordb.FalkorDB(...).connection` is a real
`redis.Redis(...)` client (`falkordb.py:150`), so every standard Redis
command -- `DUMP`/`RESTORE`/`RENAME`/`DELETE`/`pipeline()` -- is reachable
directly, not only the `GRAPH.*` subset `falkordb.Graph` wraps. `redis`
ships `py.typed` (unlike `falkordb`), so once a value is narrowed to this
module's own Protocols, no further per-call suppression is needed anywhere
else in `ps_service.export`/`ps_service.restore`.

Three Protocols, mirroring the existing `GraphHandle`/`_ConnectivityProbe`
"narrow structural stand-in + one conversion site" convention already used
three times over (`ingestion/falkordb_client.py`, `domain_mapper/
falkordb_client.py`, `company_merge/falkordb_client.py`) rather than a
fourth near-duplicate `.query()`-only Protocol:

- `_RawGraphConnection` -- the raw, single-key Redis-native primitives
  (`rename`/`delete`/`pipeline`) D8's staged-write sequence needs.
  `pipeline()` returns `_WatchablePipeline` (CHANGES.md B1) so a caller can
  WATCH/MULTI/EXEC for the `policy_system` leg's optimistic-concurrency
  retry loop (`ps_service.restore.staging.
  stage_and_finalize_policy_system_leg`). `dump`/`restore` REMOVED
  (CHANGES2.md) -- this environment's FalkorDB corrupts data across that
  command pair (IMPL_SLICE_2.4.md, independently reconfirmed twice).
- `_GraphCopyHandle` -- the `GRAPH.COPY`/Cypher-`query()` surface
  `ps_service.restore.staging.snapshot_single_tenant` needs, confirmed
  present at `falkordb/graph.py:149-160` (`Graph.copy`) and `graph.py:105-
  116` (`Graph.query`).
- `_GraphQueryHandle` -- the bare parameterized-Cypher surface
  `ps_service.export.serialize.serialize_graph` and `ps_service.restore.
  populate.populate_graph` need, nothing else. Distinct from
  `_GraphCopyHandle` (adds `.copy()`) even though a real `falkordb.Graph`
  satisfies both -- each Protocol stays scoped to exactly what its own
  caller uses, per this module's existing narrow-Protocol convention.

All three Protocols are private to this module by naming convention, but
`_RawGraphConnection`/`_GraphCopyHandle` are imported cross-module by
`ps_service.restore.staging` (CHANGES.md B1: "`ps_service.restore` imports
both Protocols from `ps_service.export.falkordb_connection`") -- consistent
with the already-established one-directional Export -> Restore dependency
(`restore/models.py`'s `RestoreArtifact` already imports
`InstrumentManifest` from `export.models`).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, cast

if TYPE_CHECKING:
    from falkordb import FalkorDB


class _WatchablePipeline(Protocol):
    """Structural stand-in for `redis.client.Pipeline`'s WATCH/MULTI/EXEC surface.

    Confirmed present only on `Pipeline` (`connection.pipeline(transaction=
    True)`'s return value, `redis/client.py:1796`), never effectively on
    the bare `redis.Redis` client -- CHANGES.md B1's empirically-verified
    fact: a bare client's own `watch()` is a deprecated no-op stub that
    never issues `WATCH` to the server. A caller must hold a single
    `Pipeline` object across `watch()` -> (a read of the watched key) ->
    `multi()` -> queued commands -> `execute()` for `WATCH` to have any
    effect; `reset()` must always run afterward (in a `finally`) to release
    the pipeline's held connection and any outstanding watches.
    """

    def watch(self, *names: str) -> None:
        """Mark `names` as watched -- a later `execute()` aborts if any changed."""
        ...

    def multi(self) -> None:
        """Switch a watched pipeline back into command-queuing mode."""
        ...

    def rename(self, src: str, dst: str) -> object:
        """Queue a `RENAME src -> dst` (Redis's native, overwrite-destination rename)."""
        ...

    def execute(self) -> list[object]:
        """Run every queued command atomically.

        Raises `redis.exceptions.WatchError` if a watched key changed
        since `watch()` was called.
        """
        ...

    def reset(self) -> None:
        """Release the pipeline's held connection and any watches. Always call in `finally`."""
        ...


class _RawGraphConnection(Protocol):
    """Structural stand-in for `falkordb.FalkorDB(...).connection` (a real `redis.Redis`).

    Narrows the full `redis.Redis` surface to exactly the raw, single-key
    primitives D8's staged-write sequence needs (PLAN.md §0.4): `RENAME`/
    `DELETE`, plus `pipeline()` for the atomic multi-`RENAME` finalize step.
    `dump`/`restore` REMOVED (CHANGES2.md) -- this environment's FalkorDB
    corrupts data across that command pair (IMPL_SLICE_2.4.md, independently
    reconfirmed twice). Only RENAME/DELETE/pipeline remain -- all
    independently confirmed clean (CHANGES.md B1/MA4; CHANGES2.md §4).
    Callers outside this module never import `redis.Redis`/
    `falkordb.FalkorDB.connection` directly -- `raw_connection` below is the
    one conversion site.
    """

    def rename(self, src: str, dst: str) -> bool:
        """Atomically rename key `src` to `dst`, silently overwriting an existing `dst`."""
        ...

    def delete(self, *names: str) -> int:
        """Delete zero or more keys. A missing name is silently skipped, never an error."""
        ...

    def pipeline(self, *, transaction: bool = True) -> _WatchablePipeline:
        """Open a pipeline that queues commands for one atomic `MULTI`/`EXEC` block."""
        ...


class _GraphCopyHandle(Protocol):
    """Structural stand-in for `falkordb.Graph`'s copy/query surface used by staging.

    Narrows to exactly what `ps_service.restore.staging.
    snapshot_single_tenant` needs: `GRAPH.COPY` (PLAN.md §0.4) and a plain
    Cypher `query()` for MA4's empty-key vivify fallback (CHANGES.md).
    Callers never import `falkordb.Graph` directly for this purpose --
    `graph_copy_handle` below is the one conversion site.
    """

    def copy(self, clone: str) -> object:
        """Clone this graph (schema included) under a new name (`GRAPH.COPY`)."""
        ...

    def query(self, q: str, params: dict[str, object] | None = None) -> object:
        """Run Cypher `q` against this graph."""
        ...


def raw_connection(db: FalkorDB) -> _RawGraphConnection:
    """Expose `db`'s underlying real `redis.Redis` client as `_RawGraphConnection`.

    The one conversion site for this Protocol: `db.connection` is already a
    real `redis.Redis(...)` instance (PLAN.md §0.4), returned unchanged --
    no wrapping, no copy.
    """
    # cast: redis-py declares RENAME/DELETE/pipeline via overloaded
    # `self: SyncClientProtocol` / `self: AsyncClientProtocol` self-types (its own
    # sync/async dispatch mechanism), which basedpyright cannot structurally match
    # against a plain external Protocol even though the real methods' call shapes
    # conform (verified live against FalkorDB, PLAN.md §0.4) -- the one unavoidable
    # cast this module needs, at its single conversion site.
    return cast("_RawGraphConnection", db.connection)


def graph_copy_handle(db: FalkorDB, name: str) -> _GraphCopyHandle:
    """Select graph `name` on `db`, typed as `_GraphCopyHandle`.

    The one conversion site for this Protocol -- mirrors `ps_service.
    company_merge.falkordb_client.select_graph`'s own "single conversion
    site" convention.
    """
    return db.select_graph(name)


class _GraphQueryHandle(Protocol):
    """Structural stand-in for `falkordb.Graph`'s bare parameterized-Cypher surface.

    Exactly what `export.serialize.serialize_graph` and `restore.populate.
    populate_graph` need, nothing else. Distinct from `_GraphCopyHandle`
    (adds `.copy()`, used only by `snapshot_single_tenant`'s GRAPH.COPY
    need) even though a real `falkordb.Graph` satisfies both -- each
    Protocol stays scoped to exactly what its own caller uses, per this
    module's existing narrow-Protocol convention.
    """

    def query(self, q: str, params: dict[str, object] | None = None) -> object:
        """Run Cypher `q` against this graph."""
        ...


def graph_query_handle(db: FalkorDB, name: str) -> _GraphQueryHandle:
    """Select graph `name` on `db`, typed as `_GraphQueryHandle`. One conversion site."""
    return db.select_graph(name)
