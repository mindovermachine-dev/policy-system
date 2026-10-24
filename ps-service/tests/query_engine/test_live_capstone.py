"""The live capstone proving the read path against real
infrastructure.

`@pytest.mark.falkordb_live`. Connects to the real, reachable FalkorDB
instance and reads a uniquely named throwaway graph the test seeds and
deletes itself -- never the shared `policy_system` graph, whose content is ambient. This
whole file is read-only by construction: every query either goes through
`execute_cypher_query`'s write-clause guard (which
`test_live_write_clause_rejected_and_graph_provably_unmutated` below proves
actually rejects before `graph.query` is ever called), or is a plain
`MATCH ... RETURN` read issued directly against the real `GraphHandle`.

Two tests:

1. `test_live_read_only_query_matches_real_driver_response_shape` -- a real
   `MATCH (n) RETURN n LIMIT 5`-shaped query via `execute_cypher_query`,
   asserting `columns` is non-empty and matches the `RETURN` clause's own
   alias(es) (a real driver-response-shape check), and that no exception is
   raised. Deliberately does NOT assert `row_count == len(rows)`
   -- see that test's own docstring for why.

2. `test_live_write_clause_rejected_and_graph_provably_unmutated` -- a
   write-clause query against the same real graph raises
   `WriteClauseRejectedError`, and a direct follow-up `MATCH (n) RETURN
   count(n)` (bypassing `execute_cypher_query`, a legitimate read) taken
   both before and after the rejected attempt proves the count is
   unchanged -- defense-in-depth proof the guard actually prevented a
   write, not just that it raised.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, cast

import pytest
from graph_gateway.live_endpoints import FALKORDB_TEST_TIMEOUTS, falkordb_endpoint

from ps_service.graph_gateway.connection import (
    connect as connect_writable,
)
from ps_service.graph_gateway.connection import (
    select_graph as select_writable_graph,
)
from ps_service.logging.emitter import EmitterConfig, LogEmitter
from ps_service.query_engine.cypher_query import execute_cypher_query
from ps_service.query_engine.errors import WriteClauseRejectedError
from ps_service.query_engine.falkordb_client import GraphHandle, connect, select_graph

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


@pytest.fixture
def emitter(tmp_path: Path) -> Iterator[LogEmitter]:
    """Mirrors the other `tests/query_engine/` unit tests' throwaway-emitter
    fixture pattern -- `execute_cypher_query` logs on every branch
    (succeeded/rejected/failed), so it needs a live emitter or a configured
    process default. Neither test here asserts on log content.
    """
    log_emitter = LogEmitter(EmitterConfig(log_path=tmp_path / "test.jsonl"))
    yield log_emitter
    log_emitter.stop()


@pytest.fixture
def real_graph() -> Iterator[GraphHandle]:
    """A uniquely named throwaway graph holding a few nodes, read through the real read-only
    `connect`/`select_graph` from `falkordb_client.py` -- no fake `GraphHandle`.

    The test never reads the shared `policy_system` graph: that graph is empty (or absent) in a
    clean environment, where the unseeded guard rightly refuses every query, so a test that
    needs content seeds its own and deletes it afterwards.
    """
    graph_name = f"query_live_capstone_{uuid.uuid4().hex[:12]}"
    host, port = falkordb_endpoint()
    write_db = connect_writable(host=host, port=port, **FALKORDB_TEST_TIMEOUTS)
    seed = select_writable_graph(write_db, graph_name)
    seed.query(
        "CREATE (:Capability {id: 'cap-1'}), (:Capability {id: 'cap-2'}), (:Role {id: 'r1'})"
    )
    try:
        yield select_graph(connect(host=host, port=port, **FALKORDB_TEST_TIMEOUTS), graph_name)
    finally:
        write_db.select_graph(graph_name).delete()


def _count_nodes(graph: GraphHandle) -> int:
    """A legitimate direct read against the real graph, bypassing
    `execute_cypher_query` -- used only to observe the real node count
    before/after the guarded write attempt below. Routing this through
    `execute_cypher_query` itself would be circular; this is a plain
    `MATCH ... RETURN` query, never anything else.

    The handle is read-only (`GRAPH.RO_QUERY`): on a graph key that does not exist it answers with
    no rows instead of `[[0]]` (and creates nothing), so no rows means a count of zero. The test
    therefore does not depend on the key having been created by an earlier run.
    """
    result = graph.query("MATCH (n) RETURN count(n)")
    rows = cast("list[list[object]]", result.result_set)
    return cast("int", rows[0][0]) if rows else 0


@pytest.mark.falkordb_live
def test_live_read_only_query_matches_real_driver_response_shape(
    real_graph: GraphHandle, emitter: LogEmitter
) -> None:
    """Live proof: a real `MATCH (n) RETURN n LIMIT 5` query against
    the throwaway graph, executed through the actual
    `execute_cypher_query` against the real FalkorDB driver, not
    a scripted fake.

    Only two assertions:
    - `columns` is non-empty and matches the `RETURN` clause's own
      alias(es) exactly -- a real driver-response-shape check (`header`
      really does come back as `[[type, name], ...]` pairs from the live
      server).
    - no exception is raised -- the whole call, including the real
      `graph.query`, completes without `execute_cypher_query` wrapping a
      driver error as `QueryEngineExecutionError`.

    Deliberately NOT asserted: `result.row_count == len(result.rows)`.
    `QueryResult.row_count` is constructed as `row_count=len(rows)` inside
    `execute_cypher_query` itself, so checking it against the very same
    `rows` object it was derived from can never fail regardless of whether
    the FalkorDB integration is actually correct -- it adds no coverage
    beyond the unit tests, which already proves the same
    arithmetic against a scripted fake. Not replaced with an
    independent-source-of-truth check (e.g. a separate `count(n)` query)
    either, because `row_count`'s definition is *by construction*
    `len(rows)` -- there is no independent ground truth to check it against
    that wouldn't just re-derive the same tautology through a second query
    This is intentional, not an oversight.
    """
    result = execute_cypher_query(
        "MATCH (n) RETURN n LIMIT 5",
        graph=real_graph,
        emitter=emitter,
        timeout_ms=5000,
        row_cap=1000,
    )

    assert result.columns, "expected at least one column back from the real driver"
    assert result.columns == ["n"], (
        "columns must match the RETURN clause's own alias(es) exactly -- real driver response shape"
    )


@pytest.mark.falkordb_live
def test_live_write_clause_rejected_and_graph_provably_unmutated(
    real_graph: GraphHandle, emitter: LogEmitter
) -> None:
    """Defense-in-depth proof against the real graph: not just
    that `execute_cypher_query` raises `WriteClauseRejectedError` for a
    write-clause query, but that the throwaway graph's node
    count is provably unchanged before vs. after the rejected attempt --
    i.e. the guard actually prevented FalkorDB from ever seeing the write,
    not merely that some exception happened to be raised.

    The before/after counts are read via a direct `real_graph.query(...)`
    call through `_count_nodes`, NOT through `execute_cypher_query` -- this
    is a legitimate read, and routing it through the very function under
    test would be circular.
    """
    count_before = _count_nodes(real_graph)

    with pytest.raises(WriteClauseRejectedError):
        execute_cypher_query(
            "CREATE (n:LiveCapstoneProbe) RETURN n",
            graph=real_graph,
            emitter=emitter,
            timeout_ms=5000,
            row_cap=1000,
        )

    count_after = _count_nodes(real_graph)
    assert count_after == count_before, (
        f"the graph's node count changed ({count_before} -> {count_after}) -- "
        "the write-clause guard must reject before graph.query is ever called"
    )
