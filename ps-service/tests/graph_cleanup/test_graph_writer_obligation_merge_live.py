"""Live FalkorDB proof of the obligation-merge writer A2 (issue #190, slice 11).

Scripted fakes prove only call shape; this is the proof the statement is valid Cypher and
that its semantics hold: SATISFIED_BY / REQUIRES union onto the survivor with no duplicate,
the survivor keeps its single HAS, the absorbed node and its edges are gone, a
`MergedObligation` marker remains, a cross-role pair writes nothing, and a guard miss leaves
the graph identical. UNVERIFIED where no FalkorDB is available (collected, deselected by
default).

Run with:
`uv run pytest ps-service/tests/graph_cleanup/test_graph_writer_obligation_merge_live.py \
    -m falkordb_live -q`
Uses a throwaway graph, deleted before and after each test.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import pytest

from ps_service.graph_cleanup.errors import GraphCleanupStaleStateError
from ps_service.graph_cleanup.graph_writer import merge_obligations
from ps_service.graph_cleanup.models import ObligationExpectedCounts
from ps_service.ingestion.falkordb_client import FalkorDB, connect, select_graph

if TYPE_CHECKING:
    from collections.abc import Iterator

    from ps_service.ingestion.falkordb_client import GraphHandle

pytestmark = pytest.mark.falkordb_live

_GRAPH = "graph_cleanup_obligation_merge_live_test"


def _delete(db: FalkorDB) -> None:
    if _GRAPH in db.list_graphs():
        db.select_graph(_GRAPH).delete()


@pytest.fixture
def graph() -> Iterator[GraphHandle]:
    db = connect(host="127.0.0.1", port=6379)
    _delete(db)
    handle = select_graph(db, _GRAPH)
    handle.query(
        "CREATE (r1:Role {id: 'r1'}), (r2:Role {id: 'r2'}), "
        "(s:Obligation {id: 's', text: 'S'}), (a:Obligation {id: 'a', text: 'A'}), "
        "(x:Obligation {id: 'x', text: 'X'}), "
        "(:Requirement {id: 'q1'}), (:Requirement {id: 'q2'}), "
        "(:Capability {id: 'c1'}), (:Capability {id: 'c2'}), "
        "(r1)-[:HAS]->(s), (r1)-[:HAS]->(a), (r2)-[:HAS]->(x)"
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


def _rows(graph: GraphHandle, query: str) -> list[list[object]]:
    return cast("list[list[object]]", graph.query(query).result_set)


_EXPECTED = ObligationExpectedCounts(satisfied=2, requires=2)
_SNAPSHOT_QUERY = (
    "MATCH (n) OPTIONAL MATCH (n)-[r]->(m) RETURN n.id, type(r), m.id ORDER BY n.id, type(r), m.id"
)


def test_edges_union_onto_the_survivor_and_the_absorbed_node_is_deleted_with_a_marker(
    graph: GraphHandle,
) -> None:
    merge_obligations(graph, survivor_id="s", absorbed_id="a", expected=_EXPECTED)

    satisfied = _rows(graph, "MATCH (q)-[:SATISFIED_BY]->(:Obligation {id:'s'}) RETURN q.id")
    required = _rows(graph, "MATCH (:Obligation {id:'s'})-[:REQUIRES]->(c) RETURN c.id")
    roles = _rows(graph, "MATCH (r:Role)-[:HAS]->(:Obligation {id:'s'}) RETURN r.id")
    assert sorted(str(r[0]) for r in satisfied) == ["q1", "q2"]
    assert sorted(str(r[0]) for r in required) == ["c1", "c2"]
    assert [str(r[0]) for r in roles] == ["r1"]
    assert _rows(graph, "MATCH (o:Obligation {id:'a'}) RETURN count(o)") == [[0]]
    assert _rows(graph, "MATCH (m:MergedObligation) RETURN m.id, m.merged_into") == [["a", "s"]]


def test_a_cross_role_pair_writes_nothing(graph: GraphHandle) -> None:
    before = _rows(graph, _SNAPSHOT_QUERY)

    with pytest.raises(GraphCleanupStaleStateError):
        merge_obligations(
            graph,
            survivor_id="s",
            absorbed_id="x",
            expected=ObligationExpectedCounts(satisfied=0, requires=0),
        )

    assert _rows(graph, _SNAPSHOT_QUERY) == before


def test_a_self_merge_writes_nothing(graph: GraphHandle) -> None:
    before = _rows(graph, _SNAPSHOT_QUERY)

    with pytest.raises(GraphCleanupStaleStateError):
        merge_obligations(graph, survivor_id="s", absorbed_id="s", expected=_EXPECTED)

    assert _rows(graph, _SNAPSHOT_QUERY) == before


@pytest.mark.parametrize(
    "expected",
    [
        ObligationExpectedCounts(satisfied=1, requires=2),
        ObligationExpectedCounts(satisfied=2, requires=1),
    ],
)
def test_a_guard_miss_leaves_the_graph_identical(
    graph: GraphHandle, expected: ObligationExpectedCounts
) -> None:
    before = _rows(graph, _SNAPSHOT_QUERY)

    with pytest.raises(GraphCleanupStaleStateError):
        merge_obligations(graph, survivor_id="s", absorbed_id="a", expected=expected)

    assert _rows(graph, _SNAPSHOT_QUERY) == before
    assert _rows(graph, "MATCH (m:MergedObligation) RETURN count(m)") == [[0]]
