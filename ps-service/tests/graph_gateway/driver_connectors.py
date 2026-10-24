"""A typed stand-in for the FalkorDB driver connector, for tests that hand in a fake database."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ps_service.graph_gateway.read_only import DriverConnector, DriverDatabase


def connector_returning(database: DriverDatabase) -> DriverConnector:
    """Return a connector that ignores its host, port and timeouts and yields `database`."""

    def connect(
        host: str, port: int, *, socket_connect_timeout: float, socket_timeout: float
    ) -> DriverDatabase:
        del host, port, socket_connect_timeout, socket_timeout
        return database

    return connect
