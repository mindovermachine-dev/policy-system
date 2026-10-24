"""Live FalkorDB proof of the capability-unmerge statement.

The production unmerge is a gateway group; this file runs the FROZEN pre-migration
statement raw (`_legacy_reference.run_frozen_unmerge_capability`) as the live anchor for the
in-memory interpreter that the equivalence tests compare the gateway group against.

Scripted fakes prove only call shape; this proves the statement is valid Cypher and that its
semantics hold: merge then unmerge returns the graph to its starting edge set, edges added to
the survivor since stay, a case-2 `GOVERNED_BY` edge moves back, and a guard miss leaves the
graph identical. UNVERIFIED where no FalkorDB is available (collected, deselected by default).

Run with:
`uv run pytest ps-service/tests/graph_cleanup/test_graph_writer_unmerge_live.py -m falkordb_live -q`
Uses a throwaway graph, deleted before and after each test.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import pytest
from graph_gateway.live_endpoints import FALKORDB_TEST_TIMEOUTS, falkordb_endpoint

from graph_cleanup._legacy_reference import (
    run_frozen_merge_capabilities,
    run_frozen_unmerge_capability,
)
from ps_service.graph_cleanup.models import CapabilityUnmergeWrite, ExpectedCounts
from ps_service.graph_gateway.connection import FalkorDB, connect, select_graph

if TYPE_CHECKING:
    from collections.abc import Iterator

    from ps_service.graph_gateway.connection import WritableGraph as GraphHandle

pytestmark = pytest.mark.falkordb_live

_GRAPH = "graph_cleanup_capability_unmerge_live_test"
_SNAPSHOT_QUERY = (
    "MATCH (n) OPTIONAL MATCH (n)-[r]->(m) RETURN n.id, type(r), m.id ORDER BY n.id, type(r), m.id"
)
_WRITE = CapabilityUnmergeWrite(
    restore_requires_ids=("o1", "o2"),
    remove_requires_ids=("o1",),
    restore_covers_ids=("pa1",),
    remove_covers_ids=("pa1",),
    restore_mitigated_ids=("rp1",),
    remove_mitigated_ids=("rp1",),
    restore_policy_id=None,
    remove_policy_edge=False,
    properties={},
)


def _delete(db: FalkorDB) -> None:
    if _GRAPH in db.list_graphs():
        db.select_graph(_GRAPH).delete()


def _rows(graph: GraphHandle, query: str) -> list[list[object]]:
    return cast("list[list[object]]", graph.query(query).result_set)


@pytest.fixture
def graph() -> Iterator[GraphHandle]:
    host, port = falkordb_endpoint()
    db = connect(host=host, port=port, **FALKORDB_TEST_TIMEOUTS)
    _delete(db)
    handle = select_graph(db, _GRAPH)
    handle.query(
        "CREATE (:Capability {id: 's', name: 'S', status: 'active'}), "
        "(:Capability {id: 'a', name: 'A', status: 'active'}), "
        "(:Obligation {id: 'o1'}), (:Obligation {id: 'o2'}), (:Obligation {id: 'o_new'}), "
        "(:PracticeArea {id: 'pa1'}), (:RiskPath {id: 'rp1'})"
    )
    for rel, label, ident, target in (
        ("REQUIRES", "Obligation", "o1", "a"),
        ("REQUIRES", "Obligation", "o2", "a"),
        ("REQUIRES", "Obligation", "o2", "s"),
        ("COVERS", "PracticeArea", "pa1", "a"),
        ("MITIGATED_BY", "RiskPath", "rp1", "a"),
    ):
        handle.query(
            f"MATCH (x:{label} {{id: $x}}), (c:Capability {{id: $c}}) CREATE (x)-[:{rel}]->(c)",
            params={"x": ident, "c": target},
        )
    try:
        yield handle
    finally:
        _delete(db)


def _merge(graph: GraphHandle) -> None:
    run_frozen_merge_capabilities(
        graph,
        survivor_id="s",
        absorbed_id="a",
        expected=ExpectedCounts(requires=2, covers=1, mitigated=1),
    )


def test_merge_then_unmerge_returns_the_graph_to_its_starting_edges(graph: GraphHandle) -> None:
    before = _rows(graph, _SNAPSHOT_QUERY)
    _merge(graph)

    run_frozen_unmerge_capability(graph, absorbed_id="a", survivor_id="s", write=_WRITE)

    assert _rows(graph, _SNAPSHOT_QUERY) == before
    assert _rows(graph, "MATCH (c:Capability {id:'a'}) RETURN c.status") == [["active"]]
    assert _rows(graph, "MATCH (:Capability)-[m:MERGED_INTO]->(:Capability) RETURN count(m)") == [
        [0]
    ]


def test_an_edge_added_to_the_survivor_since_the_merge_stays(graph: GraphHandle) -> None:
    _merge(graph)
    graph.query(
        "MATCH (o:Obligation {id:'o_new'}), (s:Capability {id:'s'}) CREATE (o)-[:REQUIRES]->(s)"
    )

    run_frozen_unmerge_capability(graph, absorbed_id="a", survivor_id="s", write=_WRITE)

    assert _rows(
        graph, "MATCH (x)-[:REQUIRES]->(:Capability {id:'s'}) RETURN x.id ORDER BY x.id"
    ) == [["o2"], ["o_new"]]


def test_a_case_two_governed_by_edge_moves_back_to_the_absorbed_capability(
    graph: GraphHandle,
) -> None:
    graph.query(
        "CREATE (:Policy {id: 'p1', title: 'P', status: 'approved'}) WITH 1 AS _ "
        "MATCH (c:Capability {id:'a'}), (p:Policy {id:'p1'}) CREATE (c)-[:GOVERNED_BY]->(p)"
    )
    run_frozen_merge_capabilities(
        graph,
        survivor_id="s",
        absorbed_id="a",
        expected=ExpectedCounts(
            requires=2,
            covers=1,
            mitigated=1,
            absorbed_governed=1,
            absorbed_policy_id="p1",
            absorbed_policy_status="approved",
        ),
    )
    write = _WRITE.model_copy(update={"restore_policy_id": "p1", "remove_policy_edge": True})

    run_frozen_unmerge_capability(graph, absorbed_id="a", survivor_id="s", write=write)

    assert _rows(
        graph, "MATCH (c:Capability)-[:GOVERNED_BY]->(:Policy) RETURN c.id ORDER BY c.id"
    ) == [["a"]]


@pytest.mark.parametrize(
    "overrides",
    [
        {"remove_requires_ids": ("o1", "o_new")},  # o_new has no REQUIRES edge to the survivor
        {"restore_requires_ids": ("o1", "o2", "o_missing")},
        {"restore_covers_ids": ("pa1", "pa_missing")},
        {"restore_policy_id": "p_missing"},
    ],
)
def test_a_guard_miss_leaves_the_graph_identical(
    graph: GraphHandle, overrides: dict[str, object]
) -> None:
    _merge(graph)
    before = _rows(graph, _SNAPSHOT_QUERY)

    assert not run_frozen_unmerge_capability(
        graph, absorbed_id="a", survivor_id="s", write=_WRITE.model_copy(update=overrides)
    )

    assert _rows(graph, _SNAPSHOT_QUERY) == before


def test_a_survivor_that_is_no_longer_active_is_a_guard_miss(graph: GraphHandle) -> None:
    _merge(graph)
    graph.query("MATCH (c:Capability {id:'s'}) SET c.status = 'merged'")

    assert not run_frozen_unmerge_capability(graph, absorbed_id="a", survivor_id="s", write=_WRITE)
