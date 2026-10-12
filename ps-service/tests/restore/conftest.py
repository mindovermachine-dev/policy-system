"""Shared fixtures for `ps_service.restore`'s `falkordb_live` tests.

Mirrors `tests/export/conftest.py`'s own fixtures exactly -- duplicated
rather than imported cross-directory, matching this test suite's existing
per-component conftest convention (`tests/api/conftest.py`, `tests/
llm_interface/conftest.py`, `tests/logging/conftest.py` each keep their own
local fixtures rather than sharing one root-level copy for everything).
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, cast

import pytest
from falkordb import (  # pyright: ignore[reportMissingTypeStubs] -- falkordb ships no py.typed marker; this is the one boundary import
    FalkorDB,
)
from graph_gateway._fakes import close_all_rigs

from ps_service.logging import configure
from restore._gateway_fakes import RestoreRig

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

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


@pytest.fixture
def restore_rig() -> Iterator[RestoreRig]:
    """The real gateway over in-memory log and graph fakes, with `restore_instrument` wired in."""
    configure()  # the gateway and the restore log through the process default emitter
    rig = RestoreRig()
    yield rig
    close_all_rigs()
