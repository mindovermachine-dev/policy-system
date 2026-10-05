"""Live FalkorDB proof of the obligation-unmerge writer A6 (issue #190, slice 16).

Scripted fakes prove only call shape; this proves the statement is valid Cypher and that its
semantics hold: merge then unmerge recreates the Obligation under its original id with its
properties, `HAS`, `SATISFIED_BY` and `REQUIRES`, removes the `MergedObligation` marker and
leaves every survivor edge in place; a guard miss leaves the graph identical. UNVERIFIED where no
FalkorDB is available (collected, deselected by default).

Run with:
`uv run pytest ps-service/tests/graph_cleanup/test_graph_writer_obligation_unmerge_live.py \
    -m falkordb_live -q`
Uses a throwaway graph, deleted before and after each test.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import pytest

from ps_service.graph_cleanup.errors import GraphCleanupStaleStateError
from ps_service.graph_cleanup.graph_writer import merge_obligations, unmerge_obligation
from ps_service.graph_cleanup.models import ObligationExpectedCounts, ObligationUnmergeWrite
from ps_service.ingestion.falkordb_client import FalkorDB, connect, select_graph

if TYPE_CHECKING:
    from collections.abc import Iterator

    from ps_service.ingestion.falkordb_client import GraphHandle

pytestmark = pytest.mark.falkordb_live

_GRAPH = "graph_cleanup_obligation_unmerge_live_test"
_SNAPSHOT_QUERY = (
    "MATCH (n) OPTIONAL MATCH (n)-[r]->(m) RETURN n.id, type(r), m.id ORDER BY n.id, type(r), m.id"
)
_WRITE = ObligationUnmergeWrite(
    role_id="r1",
    satisfied_by_ids=("q1", "q2"),
    requires_ids=("c1", "c2"),
    properties={"text": "A", "confidence": 0.8},
)


def _delete(db: FalkorDB) -> None:
    if _GRAPH in db.list_graphs():
        db.select_graph(_GRAPH).delete()


def _rows(graph: GraphHandle, query: str) -> list[list[object]]:
    return cast("list[list[object]]", graph.query(query).result_set)


@pytest.fixture
def graph() -> Iterator[GraphHandle]:
    db = connect(host="127.0.0.1", port=6379)
    _delete(db)
    handle = select_graph(db, _GRAPH)
    handle.query(
        "CREATE (r1:Role {id: 'r1'}), "
        "(s:Obligation {id: 's', text: 'S'}), "
        "(a:Obligation {id: 'a', text: 'A', confidence: 0.8}), "
        "(:Requirement {id: 'q1'}), (:Requirement {id: 'q2'}), "
        "(:Capability {id: 'c1'}), (:Capability {id: 'c2'}), "
        "(r1)-[:HAS]->(s), (r1)-[:HAS]->(a)"
    )
    for query in (
        "MATCH (q:Requirement {id:'q1'}), (o:Obligation {id:'a'}) CREATE (q)-[:SATISFIED_BY]->(o)",
        "MATCH (q:Requirement {id:'q2'}), (o:Obligation {id:'a'}) CREATE (q)-[:SATISFIED_BY]->(o)",
        "MATCH (q:Requirement {id:'q2'}), (o:Obligation {id:'s'}) CREATE (q)-[:SATISFIED_BY]->(o)",
        "MATCH (o:Obligation {id:'a'}), (c:Capability {id:'c1'}) CREATE (o)-[:REQUIRES]->(c)",
        "MATCH (o:Obligation {id:'s'}), (c:Capability {id:'c1'}) CREATE (o)-[:REQUIRES]->(c)",
        "MATCH (o:Obligation {id:'a'}), (c:Capability {id:'c2'}) CREATE (o)-[:REQUIRES]->(c)",
    ):
        handle.query(query)
    try:
        yield handle
    finally:
        _delete(db)


def _merge(graph: GraphHandle) -> None:
    merge_obligations(
        graph,
        survivor_id="s",
        absorbed_id="a",
        expected=ObligationExpectedCounts(satisfied=2, requires=2),
    )


def test_the_obligation_is_recreated_with_its_properties_and_edges_and_the_marker_is_gone(
    graph: GraphHandle,
) -> None:
    _merge(graph)

    unmerge_obligation(graph, absorbed_id="a", survivor_id="s", write=_WRITE)

    assert _rows(graph, "MATCH (o:Obligation {id:'a'}) RETURN o.text, o.confidence") == [["A", 0.8]]
    assert _rows(
        graph, "MATCH (:Role {id:'r1'})-[:HAS]->(o:Obligation {id:'a'}) RETURN count(o)"
    ) == [[1]]
    assert _rows(
        graph, "MATCH (q)-[:SATISFIED_BY]->(:Obligation {id:'a'}) RETURN q.id ORDER BY q.id"
    ) == [["q1"], ["q2"]]
    assert _rows(
        graph, "MATCH (:Obligation {id:'a'})-[:REQUIRES]->(c) RETURN c.id ORDER BY c.id"
    ) == [["c1"], ["c2"]]
    assert _rows(graph, "MATCH (m:MergedObligation) RETURN count(m)") == [[0]]


def test_the_survivors_edges_are_left_in_place(graph: GraphHandle) -> None:
    _merge(graph)

    unmerge_obligation(graph, absorbed_id="a", survivor_id="s", write=_WRITE)

    assert _rows(
        graph, "MATCH (q)-[:SATISFIED_BY]->(:Obligation {id:'s'}) RETURN q.id ORDER BY q.id"
    ) == [["q1"], ["q2"]]
    assert _rows(
        graph, "MATCH (:Obligation {id:'s'})-[:REQUIRES]->(c) RETURN c.id ORDER BY c.id"
    ) == [["c1"], ["c2"]]


@pytest.mark.parametrize(
    "write",
    [
        _WRITE.model_copy(update={"satisfied_by_ids": ("q1", "q_missing")}),
        _WRITE.model_copy(update={"requires_ids": ("c1", "c_missing")}),
        _WRITE.model_copy(update={"role_id": "r_missing"}),
    ],
)
def test_a_guard_miss_leaves_the_graph_identical(
    graph: GraphHandle, write: ObligationUnmergeWrite
) -> None:
    _merge(graph)
    before = _rows(graph, _SNAPSHOT_QUERY)

    with pytest.raises(GraphCleanupStaleStateError):
        unmerge_obligation(graph, absorbed_id="a", survivor_id="s", write=write)

    assert _rows(graph, _SNAPSHOT_QUERY) == before


def test_a_capability_tombstone_endpoint_is_a_guard_miss(graph: GraphHandle) -> None:
    _merge(graph)
    graph.query("MATCH (c:Capability {id:'c2'}) SET c.status = 'merged'")

    with pytest.raises(GraphCleanupStaleStateError):
        unmerge_obligation(graph, absorbed_id="a", survivor_id="s", write=_WRITE)


def test_an_obligation_that_exists_again_is_a_guard_miss(graph: GraphHandle) -> None:
    _merge(graph)
    graph.query("CREATE (:Obligation {id: 'a', text: 'regenerated'})")

    with pytest.raises(GraphCleanupStaleStateError):
        unmerge_obligation(graph, absorbed_id="a", survivor_id="s", write=_WRITE)


def test_a_marker_pointing_at_another_survivor_is_a_guard_miss(graph: GraphHandle) -> None:
    _merge(graph)

    with pytest.raises(GraphCleanupStaleStateError):
        unmerge_obligation(graph, absorbed_id="a", survivor_id="other", write=_WRITE)
