"""`falkordb_live` proof of the near-miss gate's "unresolved" predicate.

The gate reuses `list_pending_reviews`, whose query requires both referenced nodes to exist under
the review's `kind`. A cleanup merge deletes the node it absorbs and leaves a `MergedCapability`
marker under the absorbed id, so a review naming a merged-away side has no node of its `kind` to
match. A scripted handle cannot execute that Cypher, so only a real FalkorDB shows that a review
naming a gone node or a merged-away node does not block an export, while a live one does.

Writes only into two disposable, uniquely tokened graphs (never the real `policy_system` or any
`*_baseline`) and deletes both in a `finally` block.
"""

from __future__ import annotations

import uuid

import pytest
from graph_gateway.live_endpoints import FALKORDB_TEST_TIMEOUTS

from ps_service.config import load_config
from ps_service.export.falkordb_connection import graph_query_handle
from ps_service.export.near_miss_gate import unresolved_near_miss_review_ids
from ps_service.graph_gateway.connection import connect_from_config, select_graph
from ps_service.graph_gateway.read_only import open_read_only_database


@pytest.mark.falkordb_live
def test_only_reviews_the_merge_path_would_accept_block_an_export_live() -> None:
    config = load_config()
    db = connect_from_config(config)
    token = uuid.uuid4().hex[:12]
    baseline_name = f"s340_{token}_baseline"
    policy_name = f"s340_{token}_policy"

    try:
        baseline = select_graph(db, baseline_name)
        baseline.query("CREATE (:Capability {id: 'cap_live_a'})")
        baseline.query("CREATE (:Capability {id: 'cap_live_merged_in_baseline'})")

        policy = select_graph(db, policy_name)
        policy.query("CREATE (:Capability {id: 'cap_live_a', status: 'active'})")
        policy.query("CREATE (:Capability {id: 'cap_live_other', status: 'active'})")
        policy.query(
            "CREATE (:MergedCapability {id: 'cap_live_merged_in_baseline', "
            "merged_into: 'cap_live_other'})"
        )
        for review_id, incoming_id, nearest_id, created_at in (
            ("review_live_blocks", "cap_live_a", "cap_live_other", "2024-01-01"),
            ("review_live_node_gone", "cap_live_a", "cap_live_absent", "2024-01-02"),
            ("review_live_merged_side", "cap_live_a", "cap_live_merged_in_baseline", "2024-01-03"),
            ("review_live_other_instrument", "cap_live_other", "cap_live_other", "2024-01-04"),
        ):
            policy.query(
                "CREATE (:PendingReview {id: $id, kind: 'Capability', incoming_id: $incoming_id, "
                "incoming_text: 'incoming', nearest_existing_id: $nearest_id, "
                "nearest_existing_text: 'nearest', similarity: 0.62, created_at: $created_at})",
                params={
                    "id": review_id,
                    "incoming_id": incoming_id,
                    "nearest_id": nearest_id,
                    "created_at": created_at,
                },
            )

        read_only = open_read_only_database(
            config.falkordb_host, config.falkordb_port, **FALKORDB_TEST_TIMEOUTS
        )
        blocking = unresolved_near_miss_review_ids(
            graph_query_handle(read_only, baseline_name), select_graph(db, policy_name)
        )

        assert blocking == ("review_live_blocks",)
    finally:
        for name in (baseline_name, policy_name):
            if name in set(db.list_graphs()):
                db.select_graph(name).delete()
        assert baseline_name not in set(db.list_graphs())
        assert policy_name not in set(db.list_graphs())


@pytest.mark.falkordb_live
def test_a_role_review_blocks_an_export_only_over_the_baselines_own_roles_live() -> None:
    config = load_config()
    db = connect_from_config(config)
    token = uuid.uuid4().hex[:12]
    baseline_name = f"s342_{token}_baseline"
    policy_name = f"s342_{token}_policy"

    try:
        baseline = select_graph(db, baseline_name)
        baseline.query("CREATE (:Role {id: 'role_live_a'}), (:Role {id: 'shared_live_id'})")

        policy = select_graph(db, policy_name)
        policy.query(
            "CREATE (:Role {id: 'role_live_a'}), (:Role {id: 'role_live_b'}), "
            "(:Role {id: 'role_live_foreign_a'}), (:Role {id: 'role_live_foreign_b'}), "
            "(:Capability {id: 'shared_live_id'}), (:Capability {id: 'cap_live_other'})"
        )
        for review_id, kind, incoming_id, nearest_id, created_at in (
            ("review_role_blocks", "Role", "role_live_a", "role_live_b", "2024-01-01"),
            (
                "review_role_other_instrument",
                "Role",
                "role_live_foreign_a",
                "role_live_foreign_b",
                "2024-01-02",
            ),
            (
                "review_capability_names_role_id",
                "Capability",
                "shared_live_id",
                "cap_live_other",
                "2024-01-03",
            ),
        ):
            policy.query(
                "CREATE (:PendingReview {id: $id, kind: $kind, incoming_id: $incoming_id, "
                "incoming_text: 'incoming', nearest_existing_id: $nearest_id, "
                "nearest_existing_text: 'nearest', similarity: 0.62, created_at: $created_at})",
                params={
                    "id": review_id,
                    "kind": kind,
                    "incoming_id": incoming_id,
                    "nearest_id": nearest_id,
                    "created_at": created_at,
                },
            )

        read_only = open_read_only_database(
            config.falkordb_host, config.falkordb_port, **FALKORDB_TEST_TIMEOUTS
        )
        blocking = unresolved_near_miss_review_ids(
            graph_query_handle(read_only, baseline_name), select_graph(db, policy_name)
        )

        assert blocking == ("review_role_blocks",)
    finally:
        for name in (baseline_name, policy_name):
            if name in set(db.list_graphs()):
                db.select_graph(name).delete()
