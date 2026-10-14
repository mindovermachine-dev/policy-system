r"""Live: FalkorDB itself rejects a write that evades the textual guard.

Observed behaviour (image `falkordb/falkordb:latest`, digest 23c7a71f7c72):

- The chosen write is `CALL db.idx.fulltext.createNodeIndex('Spike','name')`. The Query Engine's
  textual guard (`\b(CREATE|MERGE|DELETE|SET|REMOVE|DROP|FOREACH)\b`) lets it through, because
  `createNodeIndex` is not the word CREATE.
- Through `GRAPH.QUERY` it SUCCEEDS and creates an index: it truly evades the guard and mutates.
- Through `GRAPH.RO_QUERY` the server rejects it with
  `graph.RO_QUERY is to be executed only on read-only queries`.
- `GRAPH.RO_QUERY` on a graph key that does not exist raises `Invalid graph operation on empty
  key` where `GRAPH.QUERY` answers `[[0]]` and creates the key; `ReadOnlyGraph` maps exactly that
  reply to an empty result, so `GraphUnseededError` is preserved.
- `db.idx.vector.createNodeIndex` is not registered on this image: not used.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

import pytest
from graph_gateway.live_endpoints import falkordb_endpoint

from ps_service.graph_gateway.connection import connect as connect_writable
from ps_service.graph_gateway.connection import select_graph as select_writable
from ps_service.graph_gateway.read_only import open_read_only_database
from ps_service.logging.emitter import EmitterConfig, LogEmitter
from ps_service.query_engine.cypher_query import execute_cypher_query, is_write_clause
from ps_service.query_engine.errors import GraphUnseededError, QueryEngineExecutionError

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from ps_service.graph_gateway.connection import FalkorDB, WritableGraph

pytestmark = pytest.mark.falkordb_live

_EVADING_WRITE = "CALL db.idx.fulltext.createNodeIndex('Spike','name')"
_SERVER_REJECTION = "graph.RO_QUERY is to be executed only on read-only queries"


@pytest.fixture
def emitter(tmp_path: Path) -> Iterator[LogEmitter]:
    log_emitter = LogEmitter(EmitterConfig(log_path=tmp_path / "test.jsonl"))
    yield log_emitter
    log_emitter.stop()


@pytest.fixture
def database() -> FalkorDB:
    host, port = falkordb_endpoint()
    return connect_writable(host, port)


@pytest.fixture
def seeded_graph_name(database: FalkorDB) -> Iterator[str]:
    """A throwaway graph with one node, deleted afterwards."""
    name = f"ro_query_live_{uuid.uuid4().hex[:10]}"
    select_writable(database, name).query("CREATE (:Seed {id: 's'})")
    yield name
    database.select_graph(name).delete()


def _index_count(graph: WritableGraph) -> int:
    return len(graph.query("CALL db.indexes()").result_set)


def test_the_evading_write_passes_the_textual_guard() -> None:
    assert is_write_clause(_EVADING_WRITE) is False


def test_the_writable_handle_would_create_the_index(
    database: FalkorDB, seeded_graph_name: str
) -> None:
    graph = select_writable(database, seeded_graph_name)

    graph.query(_EVADING_WRITE)

    assert _index_count(graph) == 1


def test_the_read_only_path_rejects_the_evading_write_and_creates_no_index(
    database: FalkorDB, seeded_graph_name: str, emitter: LogEmitter
) -> None:
    host, port = falkordb_endpoint()
    read_only = open_read_only_database(host, port).select_graph(seeded_graph_name)

    with pytest.raises(QueryEngineExecutionError) as raised:
        execute_cypher_query(
            _EVADING_WRITE, graph=read_only, emitter=emitter, timeout_ms=5000, row_cap=10
        )

    assert _SERVER_REJECTION in str(raised.value)
    assert _index_count(select_writable(database, seeded_graph_name)) == 0


def test_a_missing_graph_key_is_unseeded_not_an_execution_error(
    database: FalkorDB, emitter: LogEmitter
) -> None:
    missing = f"ro_query_missing_{uuid.uuid4().hex[:10]}"
    host, port = falkordb_endpoint()
    read_only = open_read_only_database(host, port).select_graph(missing)

    with pytest.raises(GraphUnseededError):
        execute_cypher_query(
            "MATCH (n) RETURN n", graph=read_only, emitter=emitter, timeout_ms=5000, row_cap=10
        )

    assert missing not in database.list_graphs()
