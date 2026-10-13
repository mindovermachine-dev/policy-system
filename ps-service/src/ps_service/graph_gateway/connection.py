# © 2026 Cartman ApS. All rights reserved.
"""The one place a FalkorDB client is built and its connectivity probed (issue #215).

Only the `graph_gateway` package may import `falkordb`: this module is that boundary, and the
architecture guard (`tests/graph_gateway/test_gateway_only_graph_access.py`) fails for any other
module under `ps_service` that imports the driver or constructs a client. The write-capable
`WritableGraph` handle is the gateway's own; readers get a read-only handle instead.

The per-package `falkordb_client` modules delegate here and keep their public names.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from falkordb import (  # pyright: ignore[reportMissingTypeStubs] -- falkordb ships no py.typed marker; this is the one boundary import
    FalkorDB,
)

from ps_service.graph_gateway.connectivity import (
    ConnectivityProbe,
    GraphConnectivityError,
    check_connectivity,
    check_connectivity_via,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from ps_service.config import ServiceConfig

__all__ = [
    "ConnectivityProbe",
    "FalkorDB",
    "GraphConnectivityError",
    "GraphQueryResult",
    "WritableGraph",
    "check_connectivity",
    "check_connectivity_from_config",
    "connect",
    "connect_from_config",
    "select_graph",
]


class GraphQueryResult(Protocol):
    """Structural stand-in for `falkordb.QueryResult`: the rows a query returned."""

    @property
    def result_set(self) -> list[object]:
        """The rows returned by the query."""
        ...


class WritableGraph(Protocol):
    """Structural stand-in for `falkordb.Graph`: bare Cypher, or Cypher with `params=`.

    `params` is `dict`, not `Mapping`, to match `falkordb.Graph.query`'s own invariant parameter
    type, so a real graph satisfies this Protocol structurally.
    """

    def query(self, q: str, params: dict[str, object] | None = None) -> GraphQueryResult:
        """Run Cypher `q` (optionally parameterized via `params`) and return the result."""
        ...


def connect(
    host: str, port: int, *, client_factory: Callable[..., FalkorDB] = FalkorDB
) -> FalkorDB:
    """Construct a FalkorDB client for `host`/`port`.

    The installed driver issues a real round-trip on construction (sentinel detection), so an
    unreachable server raises `redis.exceptions.ConnectionError` here, before any probe runs. Do
    not call this outside a `dependency_health`-recording boundary
    (`check_connectivity_from_config`). `client_factory` is the driver seam for tests.
    """
    return client_factory(host=host, port=port)


def connect_from_config(config: ServiceConfig) -> FalkorDB:
    """Build the FalkorDB connection from `config.falkordb_host` / `config.falkordb_port`."""
    return connect(host=config.falkordb_host, port=config.falkordb_port)


def check_connectivity_from_config(
    config: ServiceConfig,
    *,
    open_connection: Callable[[ServiceConfig], ConnectivityProbe] = connect_from_config,
) -> None:
    """Connect and verify connectivity as one guarded step (`check_connectivity_via`).

    `open_connection` is the seam the per-package delegates use to keep their own patchable
    connect name.
    """
    check_connectivity_via(config, open_connection)


def select_graph(db: FalkorDB, name: str) -> WritableGraph:
    """Select graph `name` on `db`; the single conversion from the driver's graph type."""
    return db.select_graph(name)
