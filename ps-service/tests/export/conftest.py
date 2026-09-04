"""Shared fixtures for `ps_service.export`'s `falkordb_live` tests.

Mirrors `ps_service.config.load_config()`'s own `PS_FALKORDB_HOST`/
`PS_FALKORDB_PORT` env-var/default resolution, without importing
`ServiceConfig` for a single host/port pair -- these `falkordb_live` tests
connect directly to a real FalkorDB instance, exactly as the existing
`ingestion`/`domain_mapper`/`company_merge` live tests already do.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, cast

import pytest
from falkordb import (  # pyright: ignore[reportMissingTypeStubs] -- falkordb ships no py.typed marker; this is the one boundary import
    FalkorDB,
)

if TYPE_CHECKING:
    from collections.abc import Callable

_DEFAULT_FALKORDB_HOST = "127.0.0.1"
_DEFAULT_FALKORDB_PORT = "6379"


@pytest.fixture
def live_falkordb() -> FalkorDB:
    """A real `FalkorDB` connection, reading `PS_FALKORDB_HOST`/`PS_FALKORDB_PORT`."""
    host = os.environ.get("PS_FALKORDB_HOST", _DEFAULT_FALKORDB_HOST)
    port = int(os.environ.get("PS_FALKORDB_PORT", _DEFAULT_FALKORDB_PORT))
    return FalkorDB(host=host, port=port)


@pytest.fixture
def query_result_rows() -> Callable[[FalkorDB, str, str], list[list[object]]]:
    """Factory: run Cypher against a named graph and return its typed `result_set`.

    `falkordb.Graph.query(...).result_set` is inferred as `list[Unknown]`
    under `basedpyright` strict mode (`falkordb` ships no `py.typed`
    marker) -- the one boundary cast this live-test suite needs, so
    individual tests never touch `.result_set` directly.
    """

    def _query(db: FalkorDB, graph_name: str, cypher: str) -> list[list[object]]:
        result = db.select_graph(graph_name).query(cypher)
        return cast("list[list[object]]", result.result_set)

    return _query
