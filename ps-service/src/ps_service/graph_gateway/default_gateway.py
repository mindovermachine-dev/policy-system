"""The production composition of the Graph Write Gateway (issue #206).

Builds the gateway over the real `ps_state` log store and a FalkorDB opener, both resolved
lazily: constructing the gateway contacts neither service, so a Postgres or FalkorDB outage at
startup surfaces as the gateway's per-call fail-closed errors, never as a failure to build.
"""

from __future__ import annotations

import contextlib
import threading
from typing import TYPE_CHECKING

import redis.exceptions

from ps_service.graph_gateway.connection import connect_from_config, select_graph
from ps_service.graph_gateway.errors import SwapConflictError
from ps_service.graph_gateway.gateway import GraphWriteGateway
from ps_service.graph_gateway.key_port import graph_copy_handle, raw_connection
from ps_service.graph_gateway.read_only import open_read_only_database_from_config
from ps_service.graph_gateway.store import PsycopgGraphLogStore

if TYPE_CHECKING:
    from collections.abc import Callable, Generator, Sequence

    from falkordb import FalkorDB

    from ps_service.config import ServiceConfig
    from ps_service.graph_gateway.connection import WritableGraph
    from ps_service.graph_gateway.key_port import (
        _WatchablePipeline,  # pyright: ignore[reportPrivateUsage]
    )
    from ps_service.graph_gateway.read_only import (
        DriverDatabase,
        ReadOnlyDatabase,
        ReadOnlyGraph,
    )
    from ps_service.graph_gateway.swap_port import KeySwapper


class FalkorDBGraphOpener:
    """Opens named graphs on one FalkorDB connection that is made on first use.

    A failed connect is not remembered: the next call tries again, which is what lets the
    gateway's retry and the background reconciler recover once FalkorDB is back.
    """

    def __init__(self, config: ServiceConfig) -> None:
        """Remember `config`; nothing is connected yet."""
        self._config = config
        self._db: FalkorDB | None = None
        self._lock = threading.Lock()

    def database(self) -> FalkorDB:
        """Return the shared connection, connecting first if needed.

        Raises:
            redis.exceptions.ConnectionError: FalkorDB is unreachable (the gateway treats it
                as transient).
        """
        with self._lock:
            if self._db is None:
                self._db = connect_from_config(self._config)
            return self._db

    def __call__(self, graph: str) -> WritableGraph:
        """Return the handle of `graph`, connecting first if needed.

        Raises:
            redis.exceptions.ConnectionError: FalkorDB is unreachable (the gateway treats it
                as transient).
        """
        return select_graph(self.database(), graph)


class ReadOnlyGraphOpener:
    """Opens named graphs for reading (`GRAPH.RO_QUERY` only) on a connection made on first use.

    The drift check reads the whole live graph through it. It is a connection of its own, apart
    from the writers', and a failed connect is not remembered, as for `FalkorDBGraphOpener`.
    """

    def __init__(
        self,
        config: ServiceConfig,
        *,
        connect: Callable[[str, int], DriverDatabase] | None = None,
    ) -> None:
        """Remember `config` (and the driver seam `connect`); nothing is connected yet."""
        self._config = config
        self._connect = connect
        self._db: ReadOnlyDatabase | None = None
        self._lock = threading.Lock()

    def __call__(self, graph: str) -> ReadOnlyGraph:
        """Return the read-only handle of `graph`, connecting first if needed.

        Raises:
            redis.exceptions.ConnectionError: FalkorDB is unreachable.
        """
        with self._lock:
            if self._db is None:
                self._db = open_read_only_database_from_config(self._config, connect=self._connect)
            return self._db.select_graph(graph)


_EMPTY_KEY_MESSAGE = "Invalid graph operation on empty key"
_EMPTY_GRAPH_VIVIFY = "MATCH (n) WHERE false RETURN n"


class _PipelineSwapper:
    """The swap of one `watching` block: one `MULTI`/`EXEC` of renames on the watching pipeline."""

    def __init__(self, pipe: _WatchablePipeline) -> None:
        self._pipe = pipe

    def swap_in(self, pairs: Sequence[tuple[str, str]]) -> None:
        """Rename every `(staged, live)` pair atomically.

        Raises:
            SwapConflictError: a watched key changed since the watch began; nothing renamed.
        """
        self._pipe.multi()
        for staged, live in pairs:
            self._pipe.rename(staged, live)
        try:
            self._pipe.execute()
        except redis.exceptions.WatchError as exc:
            raise SwapConflictError from exc


class FalkorDBKeyPort:
    """`GraphKeyPort` over the opener's FalkorDB connection: `GRAPH.COPY`, `DELETE`, `RENAME`.

    Only commands confirmed clean on the engine are used (never `DUMP`/`RESTORE`). The watch is a
    Redis `WATCH` held on one pipeline for the length of the block, which is what makes a write by
    a process outside the gateway between the copy and the rename abort the swap.
    """

    def __init__(self, database: Callable[[], FalkorDB]) -> None:
        """Take the lazy connection accessor; nothing is connected yet."""
        self._database = database

    def copy(self, source: str, destination: str) -> None:
        """Copy graph `source` to `destination`; a never-created `source` becomes an empty graph."""
        db = self._database()
        try:
            graph_copy_handle(db, source).copy(destination)
        except redis.exceptions.ResponseError as exc:
            if _EMPTY_KEY_MESSAGE not in str(exc):
                raise
            graph_copy_handle(db, destination).query(_EMPTY_GRAPH_VIVIFY)

    def discard(self, *names: str) -> None:
        """Delete every key in `names` that exists; a missing one is skipped."""
        if names:
            raw_connection(self._database()).delete(*names)

    @contextlib.contextmanager
    def watching(self, names: Sequence[str]) -> Generator[KeySwapper]:
        """Watch the live keys `names` for the block and offer the swap."""
        pipe = raw_connection(self._database()).pipeline(transaction=True)
        try:
            pipe.watch(*names)
            yield _PipelineSwapper(pipe)
        finally:
            pipe.reset()


def build_default_graph_write_gateway(config: ServiceConfig) -> GraphWriteGateway:
    """Build the gateway over `config`'s `ps_state` Postgres and FalkorDB; contacts neither."""
    opener = FalkorDBGraphOpener(config)
    return GraphWriteGateway(
        log_store=PsycopgGraphLogStore(config),
        graph_opener=opener,
        key_port=FalkorDBKeyPort(opener.database),
        read_graph_opener=ReadOnlyGraphOpener(config),
    )
