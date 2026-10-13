# © 2026 Cartman ApS. All rights reserved.
"""The read handle Export uses to serialize a graph (issue #215).

Export only reads: `graph_query_handle` selects a graph on the gateway's `ReadOnlyDatabase`, so
every query is sent as `GRAPH.RO_QUERY`. This module imports no driver. The raw key primitives the
gateway's swap port uses (`raw_connection`, `graph_copy_handle`, the writable query
handle) live in `ps_service.graph_gateway.key_port`; only the gateway package may reach the driver.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from ps_service.graph_gateway.read_only import ReadOnlyDatabase

__all__ = ["_GraphQueryHandle", "graph_query_handle"]


class _GraphQueryHandle(Protocol):
    """The bare parameterized-Cypher read surface `export.serialize.serialize_graph` needs.

    A `ReadOnlyGraph` satisfies it structurally.
    """

    def query(self, q: str, params: dict[str, object] | None = None) -> object:
        """Run Cypher `q` against this graph."""
        ...


def graph_query_handle(db: ReadOnlyDatabase, name: str) -> _GraphQueryHandle:
    """Select graph `name` on `db`, typed as `_GraphQueryHandle`. Every query is read-only."""
    return db.select_graph(name)
