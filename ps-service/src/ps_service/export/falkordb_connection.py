# © 2026 Cartman ApS. All rights reserved.
"""The read-only graph handle Export uses to serialize a graph.

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
        """Run Cypher `q` against this graph.

        Args:
            q: Read-only Cypher text; may use `$name` placeholders.
            params: Values for the placeholders; `None` for a query without parameters.

        Returns:
            The driver's query result, whose `result_set` holds the rows.
        """
        ...


def graph_query_handle(db: ReadOnlyDatabase, name: str) -> _GraphQueryHandle:
    """Select graph `name` on `db` and return it as a read-only query handle.

    Args:
        db: The gateway's read-only database connection.
        name: Name of the graph to select (for example a `{short}_baseline` graph).

    Returns:
        A handle whose every query is sent as a read-only graph query. Selecting a graph that
        does not exist is not an error here; queries then see an empty graph or fail at the
        driver.
    """
    return db.select_graph(name)
