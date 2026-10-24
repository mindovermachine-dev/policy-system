"""Live FalkorDB proof of the near-miss merge: the review read, the group and the redirect.

Scripted fakes prove only call shape; this proves on a real FalkorDB that the review-sides read is
valid Cypher, that the real merge group built with `review_id` applies as written (edges move
without duplicates, the loser node is deleted and a `MergedCapability` marker names the survivor,
the `PendingReview` is gone) and that `resolve_redirects` then resolves the loser id to the
survivor. The group is applied with the gateway's own applier on
the throwaway graph; no Postgres log is involved, so the gateway's log and audit linkage is not
part of this proof (the rig tests cover it). UNVERIFIED where no FalkorDB is available (collected,
deselected by default).

Run with:
`uv run pytest ps-service/tests/graph_cleanup/test_graph_writer_near_miss_merge_live.py \
    -m falkordb_live -q`
Uses a throwaway graph, deleted before and after the test.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import pytest
from graph_gateway.live_endpoints import falkordb_endpoint

from ps_service.company_merge.models import BaselineGraph, BaselineNode
from ps_service.company_merge.pending_review import choose_survivor, read_review_merge_sides
from ps_service.company_merge.redirects import resolve_redirects
from ps_service.graph_cleanup.graph_reader import read_capability_merge_state
from ps_service.graph_cleanup.graph_submission import edge_identity
from ps_service.graph_cleanup.group_builders import build_capability_merge_group
from ps_service.graph_cleanup.merge_planner import plan_capability_merge
from ps_service.graph_gateway.applier import apply_decoded
from ps_service.graph_gateway.connection import FalkorDB, connect, select_graph

if TYPE_CHECKING:
    from collections.abc import Iterator

    from ps_service.company_merge.falkordb_client import GraphHandle as CompanyMergeGraph
    from ps_service.graph_gateway.connection import WritableGraph as GraphHandle

pytestmark = pytest.mark.falkordb_live

_GRAPH = "graph_cleanup_near_miss_merge_live_test"
_REVIEW = "review_live"


def _delete(db: FalkorDB) -> None:
    if _GRAPH in db.list_graphs():
        db.select_graph(_GRAPH).delete()


@pytest.fixture
def graph() -> Iterator[GraphHandle]:
    host, port = falkordb_endpoint()
    db = connect(host=host, port=port)
    _delete(db)
    handle = select_graph(db, _GRAPH)
    handle.query(
        "CREATE (:Capability {id: 'old', name: 'Old', status: 'active', "
        "created_at: '2020-01-01T00:00:00+00:00', embedding: [1.0, 0.0]}), "
        "(:Capability {id: 'new', name: 'New', status: 'active', "
        "created_at: '2024-01-01T00:00:00+00:00', embedding: [0.8, 0.6]}), "
        "(:Obligation {id: 'o1'}), (:Obligation {id: 'o2'}), (:PracticeArea {id: 'pa1'}), "
        "(:PendingReview {id: 'review_live', kind: 'Capability', status: 'pending', "
        "incoming_id: 'new', nearest_existing_id: 'old'})"
    )
    # Each edge carries its gateway identity, the key the merge group's edge upserts match on.
    for rel, label, ident, target in (
        ("REQUIRES", "Obligation", "o1", "new"),
        ("REQUIRES", "Obligation", "o2", "new"),
        ("REQUIRES", "Obligation", "o2", "old"),
        ("COVERS", "PracticeArea", "pa1", "new"),
    ):
        handle.query(
            f"MATCH (x:{label} {{id: $x}}), (c:Capability {{id: $c}}) "
            f"CREATE (x)-[:{rel} {{identity: $identity}}]->(c)",
            params={"x": ident, "c": target, "identity": edge_identity(rel, ident, target)},
        )
    try:
        yield handle
    finally:
        _delete(db)


def _rows(graph: GraphHandle, query: str) -> list[list[object]]:
    return cast("list[list[object]]", graph.query(query).result_set)


def test_a_near_miss_merge_applies_on_a_real_graph_and_the_loser_id_is_then_resolved(
    graph: GraphHandle,
) -> None:
    sides = read_review_merge_sides(cast("CompanyMergeGraph", graph), _REVIEW)
    assert sides is not None
    survivor_id, absorbed_id = choose_survivor(sides)
    assert (survivor_id, absorbed_id) == ("old", "new")
    state = read_capability_merge_state(graph, survivor_id=survivor_id, absorbed_id=absorbed_id)
    plan = plan_capability_merge(state)
    group = build_capability_merge_group(
        state, survivor_id=survivor_id, absorbed_id=absorbed_id, review_id=_REVIEW
    )
    assert plan.preview.policy_case == 1

    apply_decoded(
        graph,
        list(enumerate(group, start=1)),
        batch_size=100,
        on_run_applied=lambda _position: None,
    )

    on_survivor = _rows(
        graph,
        "MATCH (x)-[r:REQUIRES|COVERS]->(:Capability {id:'old'}) RETURN x.id, type(r)",
    )
    assert sorted((str(i), str(t)) for i, t in on_survivor) == [
        ("o1", "REQUIRES"),
        ("o2", "REQUIRES"),
        ("pa1", "COVERS"),
    ]
    assert _rows(graph, "MATCH (x)-[r:REQUIRES|COVERS]->(:Capability {id:'new'}) RETURN x.id") == []
    assert _rows(graph, "MATCH (a:Capability {id:'new'}) RETURN count(a)") == [[0]]
    assert _rows(graph, "MATCH (m:MergedCapability {id:'new'}) RETURN m.merged_into") == [["old"]]
    assert _rows(graph, "MATCH (p:PendingReview) RETURN count(p)") == [[0]]

    resolution = resolve_redirects(
        cast("CompanyMergeGraph", graph),
        BaselineGraph(
            regulatory_instrument_id="reg_live",
            regulatory_instrument_properties={"id": "reg_live"},
            role_nodes=(),
            requirement_nodes=(),
            obligation_nodes=(),
            capability_nodes=(
                BaselineNode(id="new", properties={"name": "New", "confidence": 0.9}),
            ),
            provenance_edges=(),
            bare_edges=(),
        ),
    )
    assert resolution.mapping == {"new": "old"}
    assert resolution.baseline.capability_nodes == ()
