"""Fixtures for the `postgres_live` tests, re-exported from `persistence.provisioned_postgres`."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from persistence.provisioned_postgres import (  # pytest discovers these by name
    fresh_provisioned,
    provisioned,
    provisioned_graph_log,
)

from graph_gateway._fakes import close_all_rigs
from graph_gateway.live_endpoints import falkordb_endpoint
from graph_gateway.live_graphs import LiveGraphs
from ps_service.ingestion.falkordb_client import connect

if TYPE_CHECKING:
    from collections.abc import Iterator

__all__ = ["close_gateway_rigs", "fresh_provisioned", "provisioned", "provisioned_graph_log"]


@pytest.fixture
def live_graphs() -> Iterator[LiveGraphs]:
    """Throwaway real FalkorDB graphs (used only by `falkordb_live` tests), deleted afterwards."""
    host, port = falkordb_endpoint()
    graphs = LiveGraphs(connect(host=host, port=port))
    yield graphs
    graphs.close()


@pytest.fixture(autouse=True)
def close_gateway_rigs() -> Iterator[None]:
    """Stop the reconciler threads of every gateway rig a test built."""
    yield
    close_all_rigs()
