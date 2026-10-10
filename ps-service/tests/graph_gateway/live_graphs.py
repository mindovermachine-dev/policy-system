"""Throwaway FalkorDB graphs and a gateway over them for the `falkordb_live` tests (#207).

The log store stays the in-memory fake (Postgres has its own live files); the graph is a real,
uniquely named FalkorDB graph that the `live_graphs` fixture deletes afterwards.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, cast

from graph_gateway._fakes import InMemoryGraphLogStore
from ps_service.graph_gateway.gateway import GraphWriteGateway
from ps_service.graph_gateway.models import GroupOutcome, MutationGroup, Primitive
from ps_service.ingestion.falkordb_client import select_graph

if TYPE_CHECKING:
    from ps_service.ingestion.falkordb_client import FalkorDB, GraphHandle

AUDIT_EVENT_ID = "3f2b8c1e-5d4a-4b7e-9a61-0c2d7e8f9a10"


class LiveGraph:
    """One throwaway graph with a gateway (in-memory log) in front of it."""

    def __init__(self, db: FalkorDB, name: str) -> None:
        self.name = name
        self.db = db
        self.store = InMemoryGraphLogStore()
        self.gateway = GraphWriteGateway(
            log_store=self.store, graph_opener=lambda graph: select_graph(db, graph)
        )

    @property
    def handle(self) -> GraphHandle:
        """The real graph."""
        return select_graph(self.db, self.name)

    def submit(self, *primitives: Primitive, checkpoint_requested: bool = False) -> GroupOutcome:
        """Submit one group through the gateway."""
        return self.gateway.submit_group(
            MutationGroup(
                graph=self.name,
                audit_event_id=AUDIT_EVENT_ID,
                primitives=primitives,
                checkpoint_requested=checkpoint_requested,
            )
        )

    def rows(self, query: str, params: dict[str, object] | None = None) -> list[list[object]]:
        """Run a raw query and return its rows."""
        return cast("list[list[object]]", self.handle.query(query, params).result_set)

    def internal_id(self, label: str, node_id: str) -> int:
        """FalkorDB's internal id of the node."""
        ((value,),) = self.rows(f"MATCH (n:{label} {{id: $id}}) RETURN id(n)", {"id": node_id})
        return cast("int", value)


class LiveGraphs:
    """Hands out uniquely named graphs and deletes them all on `close`."""

    def __init__(self, db: FalkorDB) -> None:
        self._db = db
        self._graphs: list[LiveGraph] = []

    def new(self) -> LiveGraph:
        """Create a handle on a fresh, unused graph name."""
        graph = LiveGraph(self._db, f"digest_live_{uuid.uuid4().hex[:10]}")
        self._graphs.append(graph)
        return graph

    def close(self) -> None:
        """Delete every graph this object handed out."""
        existing = set(self._db.list_graphs())
        for graph in self._graphs:
            if graph.name in existing:
                self._db.select_graph(graph.name).delete()
