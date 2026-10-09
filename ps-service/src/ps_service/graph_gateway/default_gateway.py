"""The production composition of the Graph Write Gateway (issue #206).

Builds the gateway over the real `ps_state` log store and a FalkorDB opener, both resolved
lazily: constructing the gateway contacts neither service, so a Postgres or FalkorDB outage at
startup surfaces as the gateway's per-call fail-closed errors, never as a failure to build.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

from ps_service.graph_gateway.gateway import GraphWriteGateway
from ps_service.graph_gateway.store import PsycopgGraphLogStore
from ps_service.ingestion.falkordb_client import connect_from_config, select_graph

if TYPE_CHECKING:
    from falkordb import FalkorDB

    from ps_service.config import ServiceConfig
    from ps_service.ingestion.falkordb_client import GraphHandle


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

    def __call__(self, graph: str) -> GraphHandle:
        """Return the handle of `graph`, connecting first if needed.

        Raises:
            redis.exceptions.ConnectionError: FalkorDB is unreachable (the gateway treats it
                as transient).
        """
        with self._lock:
            if self._db is None:
                self._db = connect_from_config(self._config)
            db = self._db
        return select_graph(db, graph)


def build_default_graph_write_gateway(config: ServiceConfig) -> GraphWriteGateway:
    """Build the gateway over `config`'s `ps_state` Postgres and FalkorDB; contacts neither."""
    return GraphWriteGateway(
        log_store=PsycopgGraphLogStore(config), graph_opener=FalkorDBGraphOpener(config)
    )
