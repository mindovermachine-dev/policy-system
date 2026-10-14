"""Live FalkorDB proof of the FROZEN capability-merge statement.

Scripted fakes prove only call shape; this is the proof the statement is valid Cypher
and that its semantics hold: edges move with no duplicate, the absorbed node is kept as a
`merged` tombstone with `MERGED_INTO`, an active-status filter does not match it, and a
guard miss leaves the graph identical. UNVERIFIED where no FalkorDB is available (collected,
deselected by default).

A capability merge is a gateway group; the pre-migration statement lives in
`_legacy_reference` and is run raw here, so the semantics the reference's interpreter models
(and the equivalence tests compare against) stay anchored on a real FalkorDB.

Run with:
`uv run pytest ps-service/tests/graph_cleanup/test_graph_writer_capability_merge_live.py \
    -m falkordb_live -q`
Uses a throwaway graph, deleted before and after each test.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import pytest

from graph_cleanup._legacy_reference import FROZEN_STATEMENTS, run_frozen_merge_capabilities
from ps_service.graph_cleanup.errors import GraphCleanupStaleStateError
from ps_service.graph_cleanup.models import ExpectedCounts
from ps_service.graph_gateway.connection import FalkorDB, connect, select_graph

if TYPE_CHECKING:
    from collections.abc import Iterator

    from ps_service.graph_gateway.connection import WritableGraph as GraphHandle

pytestmark = pytest.mark.falkordb_live

_GRAPH = "graph_cleanup_capability_merge_live_test"


def _frozen_merge_capabilities(
    graph: GraphHandle, *, survivor_id: str, absorbed_id: str, expected: ExpectedCounts
) -> None:
    """Run the FROZEN pre-migration merge statement raw; no row is a guard miss."""
    if not run_frozen_merge_capabilities(
        graph, survivor_id=survivor_id, absorbed_id=absorbed_id, expected=expected
    ):
        message = "the capabilities changed since the preview; nothing was written"
        raise GraphCleanupStaleStateError(message)


def _delete(db: FalkorDB) -> None:
    if _GRAPH in db.list_graphs():
        db.select_graph(_GRAPH).delete()


@pytest.fixture
def graph() -> Iterator[GraphHandle]:
    db = connect(host="127.0.0.1", port=6379)
    _delete(db)
    handle = select_graph(db, _GRAPH)
    handle.query(
        "CREATE (:Capability {id: 's', name: 'S', status: 'active'}), "
        "(:Capability {id: 'a', name: 'A', status: 'active'}), "
        "(:Obligation {id: 'o1'}), (:Obligation {id: 'o2'}), "
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


def _rows(graph: GraphHandle, query: str) -> list[list[object]]:
    return cast("list[list[object]]", graph.query(query).result_set)


_EXPECTED = ExpectedCounts(requires=2, covers=1, mitigated=1)
_SNAPSHOT_QUERY = (
    "MATCH (n) OPTIONAL MATCH (n)-[r]->(m) RETURN n.id, type(r), m.id ORDER BY n.id, type(r), m.id"
)


def test_edges_move_without_duplicates_and_the_absorbed_node_becomes_a_tombstone(
    graph: GraphHandle,
) -> None:
    _frozen_merge_capabilities(graph, survivor_id="s", absorbed_id="a", expected=_EXPECTED)

    on_survivor = _rows(
        graph,
        "MATCH (x)-[r:REQUIRES|COVERS|MITIGATED_BY]->(:Capability {id:'s'}) RETURN x.id, type(r)",
    )
    assert sorted((str(i), str(t)) for i, t in on_survivor) == [
        ("o1", "REQUIRES"),
        ("o2", "REQUIRES"),
        ("pa1", "COVERS"),
        ("rp1", "MITIGATED_BY"),
    ]
    assert (
        _rows(
            graph, "MATCH (x)-[r:REQUIRES|COVERS|MITIGATED_BY]->(:Capability {id:'a'}) RETURN x.id"
        )
        == []
    )
    assert _rows(
        graph,
        "MATCH (a:Capability {id:'a'})-[:MERGED_INTO]->(s:Capability {id:'s'}) RETURN a.status",
    ) == [["merged"]]
    assert _rows(
        graph, "MATCH (c:Capability) WHERE coalesce(c.status,'active')='active' RETURN c.id"
    ) == [["s"]]


@pytest.mark.parametrize(
    "expected",
    [
        ExpectedCounts(requires=1, covers=1, mitigated=1),
        ExpectedCounts(requires=2, covers=0, mitigated=1),
        ExpectedCounts(requires=2, covers=1, mitigated=0),
        ExpectedCounts(requires=2, covers=1, mitigated=1, absorbed_governed=1),
    ],
)
def test_a_guard_miss_leaves_the_graph_identical(
    graph: GraphHandle, expected: ExpectedCounts
) -> None:
    before = _rows(
        graph,
        _SNAPSHOT_QUERY,
    )

    with pytest.raises(GraphCleanupStaleStateError):
        _frozen_merge_capabilities(graph, survivor_id="s", absorbed_id="a", expected=expected)

    assert before == _rows(
        graph,
        _SNAPSHOT_QUERY,
    )


def test_a_status_change_of_either_node_is_a_guard_miss(graph: GraphHandle) -> None:
    graph.query("MATCH (c:Capability {id:'s'}) SET c.status = 'deprecated'")

    with pytest.raises(GraphCleanupStaleStateError):
        _frozen_merge_capabilities(graph, survivor_id="s", absorbed_id="a", expected=_EXPECTED)


def _add_policy(graph: GraphHandle, *, capability: str, status: str = "approved") -> None:
    graph.query(
        "MERGE (p:Policy {id: 'p1'}) SET p.title = 'P', p.status = $status "
        "WITH p MATCH (c:Capability {id: $c}) CREATE (c)-[:GOVERNED_BY]->(p)",
        params={"status": status, "c": capability},
    )


def _governors(graph: GraphHandle) -> list[list[object]]:
    return _rows(
        graph, "MATCH (c:Capability)-[:GOVERNED_BY]->(p:Policy) RETURN c.id, p.id ORDER BY c.id"
    )


def _case2(
    *,
    absorbed: bool,
    status: str = "approved",
) -> ExpectedCounts:
    return ExpectedCounts(
        requires=2,
        covers=1,
        mitigated=1,
        absorbed_governed=1 if absorbed else 0,
        survivor_governed=0 if absorbed else 1,
        absorbed_policy_id="p1" if absorbed else None,
        absorbed_policy_status=status if absorbed else None,
        survivor_policy_id=None if absorbed else "p1",
        survivor_policy_status=None if absorbed else status,
    )


def test_case_two_an_absorbed_governed_capability_hands_its_governed_by_to_the_survivor(
    graph: GraphHandle,
) -> None:
    _add_policy(graph, capability="a")

    _frozen_merge_capabilities(
        graph, survivor_id="s", absorbed_id="a", expected=_case2(absorbed=True)
    )

    assert _governors(graph) == [["s", "p1"]]


def test_case_two_a_governed_survivor_keeps_its_governed_by_and_the_absorbed_has_none(
    graph: GraphHandle,
) -> None:
    _add_policy(graph, capability="s")

    _frozen_merge_capabilities(
        graph, survivor_id="s", absorbed_id="a", expected=_case2(absorbed=False)
    )

    assert _governors(graph) == [["s", "p1"]]


def test_case_two_a_policy_status_change_since_the_preview_is_a_guard_miss(
    graph: GraphHandle,
) -> None:
    _add_policy(graph, capability="a", status="deprecated")
    before = _rows(graph, _SNAPSHOT_QUERY)

    with pytest.raises(GraphCleanupStaleStateError):
        _frozen_merge_capabilities(
            graph, survivor_id="s", absorbed_id="a", expected=_case2(absorbed=True)
        )

    assert _rows(graph, _SNAPSHOT_QUERY) == before


def test_case_three_the_same_policy_deletes_only_the_absorbed_governed_by(
    graph: GraphHandle,
) -> None:
    _add_policy(graph, capability="s")
    graph.query(
        "MATCH (c:Capability {id: 'a'}), (p:Policy {id: 'p1'}) CREATE (c)-[:GOVERNED_BY]->(p)"
    )
    expected = ExpectedCounts(
        requires=2,
        covers=1,
        mitigated=1,
        absorbed_governed=1,
        survivor_governed=1,
        absorbed_policy_id="p1",
        absorbed_policy_status="approved",
        survivor_policy_id="p1",
        survivor_policy_status="approved",
    )

    _frozen_merge_capabilities(graph, survivor_id="s", absorbed_id="a", expected=expected)

    assert _governors(graph) == [["s", "p1"]]


def test_case_three_different_policies_are_a_guard_miss(graph: GraphHandle) -> None:
    _add_policy(graph, capability="s")
    graph.query(
        "MERGE (q:Policy {id: 'p2'}) SET q.title = 'Q', q.status = 'approved' "
        "WITH q MATCH (c:Capability {id: 'a'}) CREATE (c)-[:GOVERNED_BY]->(q)"
    )
    expected = ExpectedCounts(
        requires=2,
        covers=1,
        mitigated=1,
        absorbed_governed=1,
        survivor_governed=1,
        absorbed_policy_id="p1",
        absorbed_policy_status="approved",
        survivor_policy_id="p1",
        survivor_policy_status="approved",
    )
    before = _rows(graph, _SNAPSHOT_QUERY)

    with pytest.raises(GraphCleanupStaleStateError):
        _frozen_merge_capabilities(graph, survivor_id="s", absorbed_id="a", expected=expected)

    assert _rows(graph, _SNAPSHOT_QUERY) == before


def _frozen_release(graph: GraphHandle, *, capability_id: str, policy_id: str) -> bool:
    """Run the FROZEN pre-migration release statement raw; `True` when it matched.

    A release is a gateway group; the statement is kept in `_legacy_reference` as the
    reference the equivalence tests compare against, and these live tests anchor the semantics
    the reference's interpreter models.
    """
    params: dict[str, object] = {"capability_id": capability_id, "policy_id": policy_id}
    rows = graph.query(FROZEN_STATEMENTS["release_governance"], params).result_set
    return bool(rows)


def test_release_governance_deletes_the_edge_of_a_draft_policy(graph: GraphHandle) -> None:
    _add_policy(graph, capability="s", status="draft")

    assert _frozen_release(graph, capability_id="s", policy_id="p1") is True

    assert _governors(graph) == []
    assert _rows(graph, "MATCH (p:Policy {id: 'p1'}) RETURN count(p)") == [[1]]


@pytest.mark.parametrize("status", ["proposed", "approved", "deprecated"])
def test_release_governance_on_a_non_draft_policy_is_a_guard_miss(
    graph: GraphHandle, status: str
) -> None:
    _add_policy(graph, capability="s", status=status)

    assert _frozen_release(graph, capability_id="s", policy_id="p1") is False

    assert _governors(graph) == [["s", "p1"]]


def test_release_governance_leaves_other_capabilities_of_the_policy_alone(
    graph: GraphHandle,
) -> None:
    _add_policy(graph, capability="s", status="draft")
    graph.query(
        "MATCH (c:Capability {id: 'a'}), (p:Policy {id: 'p1'}) CREATE (c)-[:GOVERNED_BY]->(p)"
    )

    assert _frozen_release(graph, capability_id="s", policy_id="p1") is True

    assert _governors(graph) == [["a", "p1"]]
