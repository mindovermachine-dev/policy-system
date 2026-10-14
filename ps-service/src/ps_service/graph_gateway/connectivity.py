# © 2026 Cartman ApS. All rights reserved.
"""The FalkorDB connectivity probe, free of the driver.

`check_connectivity` asks a connection to list its graphs (the cheapest real round-trip) and
records the outcome in `dependency_health`. It lives apart from `connection.py` (the one module
that imports the driver) so a reader package can probe a read-only connection without importing the
write-capable factory. `connection.py` re-exports these names.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from ps_service.dependency_health import FALKORDB, mark_healthy, mark_unhealthy

if TYPE_CHECKING:
    from collections.abc import Callable

    from ps_service.config import ServiceConfig

__all__ = [
    "ConnectivityProbe",
    "GraphConnectivityError",
    "check_connectivity",
    "check_connectivity_via",
]


class GraphConnectivityError(Exception):
    """FalkorDB could not be reached; the message names host and port and carries the cause."""


class ConnectivityProbe(Protocol):
    """The one call a connectivity check needs; a real client satisfies it structurally."""

    def list_graphs(self) -> list[str]:
        """List the names of the graphs on the server.

        Returns:
            The graph names; empty when the server holds none.
        """
        ...


def _unreachable(host: str, port: int, cause: Exception) -> GraphConnectivityError:
    """Build the error that names the unreachable host and port and the cause."""
    return GraphConnectivityError(
        f"FalkorDB connection failed at {host}:{port}. Is FalkorDB running? Error: {cause}"
    )


def check_connectivity(db: ConnectivityProbe, host: str, port: int) -> None:
    """Fail loud before real work: `db` must list its graphs (the cheapest real round-trip).

    Records the outcome in `dependency_health` either way, so the `/ready` probe and the write
    paths' own exception handling feed one signal.

    Args:
        db: The connection to probe.
        host: Server host, used only in the error message.
        port: Server port, used only in the error message.

    Raises:
        GraphConnectivityError: `db` could not list its graphs (the cause is chained).

    Side Effects:
        Lists the server's graphs and marks the `FALKORDB` dependency healthy or unhealthy.
    """
    try:
        db.list_graphs()
    except Exception as exc:
        mark_unhealthy(FALKORDB, error=exc)
        raise _unreachable(host, port, exc) from exc
    mark_healthy(FALKORDB)


def check_connectivity_via(
    config: ServiceConfig, open_connection: Callable[[ServiceConfig], ConnectivityProbe]
) -> None:
    """Connect with `open_connection` and verify connectivity as one guarded step.

    A failure from either step is recorded in `dependency_health`; connecting performs a real
    round-trip, so it can fail before the probe runs.

    Args:
        config: Service configuration naming the FalkorDB host and port.
        open_connection: Opens the connection from `config`.

    Raises:
        GraphConnectivityError: the connection or the probe failed (the cause is chained).

    Side Effects:
        Opens a connection, lists the server's graphs and marks the `FALKORDB` dependency healthy
        or unhealthy.
    """
    try:
        db = open_connection(config)
    except Exception as exc:
        mark_unhealthy(FALKORDB, error=exc)
        raise _unreachable(config.falkordb_host, config.falkordb_port, exc) from exc
    check_connectivity(db, config.falkordb_host, config.falkordb_port)
