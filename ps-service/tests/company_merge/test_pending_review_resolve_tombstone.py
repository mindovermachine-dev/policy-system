"""Near-miss merge and `merged` Capability tombstones (issue #190).

Two guarantees. First, a `PendingReview` naming a tombstone on either side is
stale: `resolve_review` raises `StalePendingReviewError` before anything is submitted, so the
tombstone and its `MERGED_INTO` redirect survive. Second, the merge group re-points every inbound
`MERGED_INTO` edge from the deleted loser onto the winner (gateway entries here; the real graph is
proven by the `falkordb_live` test at the bottom, not runnable in the implementation sandbox).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import pytest

from company_merge._gateway_fakes import POLICY_SYSTEM, UnusedSubmitter
from ps_service.company_merge.errors import StalePendingReviewError
from ps_service.company_merge.graph_submission import GraphWriteTarget
from ps_service.company_merge.pending_review import resolve_review
from ps_service.graph_gateway.connection import connect_from_config, select_graph
from ps_service.graph_gateway.default_gateway import build_default_graph_write_gateway
from ps_service.graph_gateway.system_audit import build_system_audit_recorder

if TYPE_CHECKING:
    from persistence.provisioned_postgres import Provisioned

    from company_merge._fakes import MakeEmitter
    from company_merge._gateway_fakes import MergeRig
    from ps_service.config import ServiceConfig

_REVIEW = "review_aaa"


def _seed(rig: MergeRig, *, incoming: str, existing: str, merged: tuple[str, ...] = ()) -> None:
    graph = rig.policy_system
    for node_id in (incoming, existing):
        properties: dict[str, object] = {"name": node_id, "created_at": "2020-01-01T00:00:00+00:00"}
        if node_id in merged:
            properties["status"] = "merged"
        graph.seed_node(("Capability",), node_id, properties)
    graph.seed_node(
        ("PendingReview",),
        _REVIEW,
        {"kind": "Capability", "incoming_id": incoming, "nearest_existing_id": existing},
    )


@pytest.mark.parametrize(
    ("incoming_id", "existing_id", "merged_ids"),
    [
        pytest.param("cap_tomb", "cap_live", ("cap_tomb",), id="incoming-is-tombstone"),
        pytest.param("cap_live", "cap_tomb", ("cap_tomb",), id="existing-is-tombstone"),
    ],
)
def test_review_naming_a_tombstone_is_stale_and_submits_nothing(
    incoming_id: str, existing_id: str, merged_ids: tuple[str, ...], merge_rig: MergeRig
) -> None:
    _seed(merge_rig, incoming=incoming_id, existing=existing_id, merged=merged_ids)
    target = GraphWriteTarget(UnusedSubmitter(), POLICY_SYSTEM, merge_rig.audit_event_id)

    with pytest.raises(StalePendingReviewError):
        resolve_review(merge_rig.legacy, _REVIEW, "merge", target)

    assert merge_rig.logged() == []
    assert ("Capability", "cap_tomb") in merge_rig.policy_system.nodes


def test_review_between_two_active_capabilities_still_merges(
    make_emitter: MakeEmitter, merge_rig: MergeRig
) -> None:
    emitter, _ = make_emitter()
    _seed(merge_rig, incoming="cap_a", existing="cap_b")
    target = GraphWriteTarget(merge_rig.gateway, POLICY_SYSTEM, merge_rig.audit_event_id)

    outcome = resolve_review(merge_rig.legacy, _REVIEW, "merge", target, emitter=emitter)

    assert outcome is not None
    # Equal `created_at` is a tie, which goes to the incoming node: the existing one is the loser.
    assert (outcome.winner_id, outcome.loser_id) == ("cap_a", "cap_b")
    assert ("Capability", "cap_b") not in merge_rig.policy_system.nodes


def test_existence_read_carries_the_predicate_on_both_sides(
    make_emitter: MakeEmitter, merge_rig: MergeRig
) -> None:
    emitter, _ = make_emitter()
    _seed(merge_rig, incoming="cap_a", existing="cap_b")
    target = GraphWriteTarget(merge_rig.gateway, POLICY_SYSTEM, merge_rig.audit_event_id)

    resolve_review(merge_rig.legacy, _REVIEW, "merge", target, emitter=emitter)

    existence = next(q for q in merge_rig.legacy.statements if "incoming_exists" in q)
    assert existence.count("<> 'merged'") == 2


def test_loser_with_inbound_tombstone_repoints_the_tombstone_to_the_winner(
    make_emitter: MakeEmitter, merge_rig: MergeRig
) -> None:
    """S absorbed T earlier, then loses a near-miss to W: T's `MERGED_INTO` moves onto W."""
    emitter, _ = make_emitter()
    graph = merge_rig.policy_system
    graph.seed_node(
        ("Capability",), "cap_w", {"name": "W", "created_at": "2020-01-01T00:00:00+00:00"}
    )
    graph.seed_node(
        ("Capability",), "cap_s", {"name": "S", "created_at": "2024-01-01T00:00:00+00:00"}
    )
    graph.seed_node(("Capability",), "cap_t", {"name": "T", "status": "merged"})
    graph.seed_legacy_edge(("MERGED_INTO", "Capability", "cap_t", "Capability", "cap_s"), {})
    graph.seed_node(
        ("PendingReview",),
        _REVIEW,
        {"kind": "Capability", "incoming_id": "cap_s", "nearest_existing_id": "cap_w"},
    )
    target = GraphWriteTarget(merge_rig.gateway, POLICY_SYSTEM, merge_rig.audit_event_id)

    outcome = resolve_review(merge_rig.legacy, _REVIEW, "merge", target, emitter=emitter)

    assert outcome is not None
    assert (outcome.winner_id, outcome.loser_id) == ("cap_w", "cap_s")
    assert {key[:5] for key in graph.edges if key[0] == "MERGED_INTO"} == {
        ("MERGED_INTO", "Capability", "cap_t", "Capability", "cap_w")
    }


def _live_target(config: ServiceConfig, graph_name: str) -> GraphWriteTarget:
    """The real gateway on the disposable graph, under one system audit row (issue #210)."""
    audit_event_id = build_system_audit_recorder(config)(
        action="ingestion_run.submit",
        resource_type="ingestion_run",
        resource_id=f"live-{graph_name}",
        details={"trigger": "live_resolve_review"},
    )
    return GraphWriteTarget(build_default_graph_write_gateway(config), graph_name, audit_event_id)


_LIVE_TEST_GRAPH = "policy_system_issue190_near_miss_tombstone_live_test"


@pytest.mark.falkordb_live
@pytest.mark.postgres_live
def test_loser_with_inbound_tombstone_leaves_the_tombstone_resolving_to_the_winner_live(
    provisioned_graph_log: Provisioned, make_emitter: MakeEmitter
) -> None:
    """Real FalkorDB: S absorbed T earlier, then loses a near-miss to W; T must resolve to W."""
    emitter, _log_path = make_emitter()
    config = provisioned_graph_log.state_config()
    db = connect_from_config(config)
    if _LIVE_TEST_GRAPH in set(db.list_graphs()):
        db.select_graph(_LIVE_TEST_GRAPH).delete()
    try:
        graph = select_graph(db, _LIVE_TEST_GRAPH)
        graph.query(
            "CREATE (:Capability {id: 'cap_w', name: 'W', status: 'active', "
            "created_at: '2020-01-01T00:00:00+00:00'}), "
            "(s:Capability {id: 'cap_s', name: 'S', status: 'active', "
            "created_at: '2024-01-01T00:00:00+00:00'}), "
            "(t:Capability {id: 'cap_t', name: 'T', status: 'merged'}), "
            "(t)-[:MERGED_INTO]->(s), "
            "(:PendingReview {id: 'review_t190', kind: 'Capability', status: 'pending', "
            "incoming_id: 'cap_s', incoming_text: 'S', nearest_existing_id: 'cap_w', "
            "nearest_existing_text: 'W', similarity: 0.9, created_at: '2024-06-01T00:00:00+00:00'})"
        )

        outcome = resolve_review(
            graph, "review_t190", "merge", _live_target(config, _LIVE_TEST_GRAPH), emitter=emitter
        )

        assert outcome is not None
        assert (outcome.winner_id, outcome.loser_id) == ("cap_w", "cap_s")
        rows = cast(
            "list[list[object]]",
            graph.query(
                "MATCH (t:Capability {id: 'cap_t'})-[:MERGED_INTO]->(x:Capability) RETURN x.id"
            ).result_set,
        )
        assert rows == [["cap_w"]]
    finally:
        db.select_graph(_LIVE_TEST_GRAPH).delete()
