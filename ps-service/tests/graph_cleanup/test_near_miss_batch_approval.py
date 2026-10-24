"""Creating a near-miss merge batch: one approval that binds every chosen merge, no graph write.

Over a `CleanupRig` (real reader, planner and approval creation; only the approval store is a
double) these tests create a batch for pending reviews and look at the signed arguments, the
returned preview and the graph afterwards.
"""

from __future__ import annotations

import copy
from typing import TYPE_CHECKING, cast

from graph_cleanup._fakes import ABSORBED, SURVIVOR, FakeApprovalStore, approval_row_count
from graph_cleanup._merge_runs import OFFICER
from graph_cleanup._near_miss_batch_worlds import (
    NEW,
    OLD,
    seed_capability_pair_with_review,
    seed_role_pair_with_review,
)
from ps_service.graph_cleanup.near_miss_batch import create_near_miss_merge_batch

if TYPE_CHECKING:
    from graph_cleanup._gateway_fakes import CleanupRig

_REVIEW = "review_aaa"
_OLD = "2020-01-01T00:00:00+00:00"
_NEW = "2024-01-01T00:00:00+00:00"
_BASE_URL = "https://ps.example.com"


def _world(rig: CleanupRig) -> None:
    """ABSORBED is the incoming node, SURVIVOR the existing and older one (the default winner)."""
    rig.seed_capability_merge_world()
    rig.seed_near_miss_review(
        _REVIEW,
        incoming_id=ABSORBED,
        existing_id=SURVIVOR,
        incoming_created_at=_NEW,
        existing_created_at=_OLD,
    )


def test_a_batch_of_one_capability_review_creates_one_approval_signing_the_item_and_the_digest(
    cleanup_rig: CleanupRig,
) -> None:
    _world(cleanup_rig)
    store = FakeApprovalStore()

    approval = create_near_miss_merge_batch(
        cleanup_rig.read,
        review_ids=[_REVIEW],
        acknowledged_review_ids=[],
        actor=OFFICER,
        base_url=_BASE_URL,
        store=store,
    )

    assert approval_row_count(store) == 1
    row = store.get_by_id(approval.pending_approval_id)
    assert row is not None
    [entry] = approval.merges
    assert entry.preview.state_digest
    assert row.tool_name == "merge-near-misses"
    assert (row.actor_subject, row.actor_issuer) == OFFICER
    assert row.normalized_args == {
        "merges": [
            {
                "review_id": _REVIEW,
                "kind": "Capability",
                "survivor_id": SURVIVOR,
                "absorbed_id": ABSORBED,
                "state_digest": entry.preview.state_digest,
                "acknowledge_governance_change": False,
            }
        ]
    }
    assert approval.approval_url.startswith(f"{_BASE_URL}/approvals/{row.id}#")
    assert approval.expires_at == row.expires_at.isoformat()


def test_creating_the_batch_leaves_the_graph_and_the_review_untouched(
    cleanup_rig: CleanupRig,
) -> None:
    _world(cleanup_rig)
    before = (copy.deepcopy(dict(cleanup_rig.graph.nodes)), set(cleanup_rig.graph.edges))

    create_near_miss_merge_batch(
        cleanup_rig.read,
        review_ids=[_REVIEW],
        acknowledged_review_ids=[],
        actor=OFFICER,
        base_url=_BASE_URL,
        store=FakeApprovalStore(),
    )

    assert (copy.deepcopy(dict(cleanup_rig.graph.nodes)), set(cleanup_rig.graph.edges)) == before
    assert ("PendingReview", _REVIEW) in cleanup_rig.graph.nodes
    assert cleanup_rig.logged_groups == []


def _create(rig: CleanupRig, store: FakeApprovalStore, review_ids: list[str]):
    return create_near_miss_merge_batch(
        rig.read,
        review_ids=review_ids,
        acknowledged_review_ids=[],
        actor=OFFICER,
        base_url=_BASE_URL,
        store=store,
    )


def test_a_mixed_batch_lists_one_preview_entry_per_review_with_the_fields_of_its_kind(
    cleanup_rig: CleanupRig,
) -> None:
    capability = seed_capability_pair_with_review(cleanup_rig, 1)
    role = seed_role_pair_with_review(cleanup_rig, 1)
    store = FakeApprovalStore()

    approval = _create(cleanup_rig, store, [role.review_id, capability.review_id])

    assert approval_row_count(store) == 1
    role_entry, capability_entry = (entry.wire_form() for entry in approval.merges)
    assert role_entry["review_id"] == role.review_id
    assert role_entry["kind"] == "Role"
    assert role_entry["moved_obligation_ids"] == ["obl_role_ex1"]
    assert role_entry["instrument_id"] == "inst_1"
    assert "policy_case" not in role_entry
    assert capability_entry["review_id"] == capability.review_id
    assert capability_entry["kind"] == "Capability"
    assert capability_entry["policy_case"] == 1
    assert capability_entry["edges_to_move"] == {
        "requires": ["obl_cap_1"],
        "covers": ["pa_cap_1"],
        "mitigated_by": [],
    }
    for entry in (role_entry, capability_entry):
        assert entry["state_digest"]
        assert entry["acknowledged"] is False
    row = store.get_by_id(approval.pending_approval_id)
    assert row is not None
    signed = cast("list[dict[str, object]]", row.normalized_args["merges"])
    assert [item["review_id"] for item in signed] == [role.review_id, capability.review_id]
    assert row.display_summary["kind"] == "near-miss-merge-batch"
    assert row.display_summary["merges"] == [role_entry, capability_entry]


def test_the_survivor_of_each_entry_follows_the_unchanged_survivor_rule(
    cleanup_rig: CleanupRig,
) -> None:
    older_existing = seed_capability_pair_with_review(cleanup_rig, 1)
    # The incoming node is the older one, so it survives.
    cleanup_rig.seed_capability("cap_s2", name="Existing", status="active")
    cleanup_rig.seed_capability("cap_a2", name="Incoming", status="active")
    cleanup_rig.seed_near_miss_review(
        "review_cap_2",
        incoming_id="cap_a2",
        existing_id="cap_s2",
        incoming_created_at=OLD,
        existing_created_at=NEW,
    )
    # A tie goes to the incoming node.
    cleanup_rig.seed_capability("cap_s3", name="Existing", status="active")
    cleanup_rig.seed_capability("cap_a3", name="Incoming", status="active")
    cleanup_rig.seed_near_miss_review(
        "review_cap_3",
        incoming_id="cap_a3",
        existing_id="cap_s3",
        incoming_created_at=OLD,
        existing_created_at=OLD,
    )
    role = seed_role_pair_with_review(cleanup_rig, 1)

    approval = _create(
        cleanup_rig,
        FakeApprovalStore(),
        [older_existing.review_id, "review_cap_2", "review_cap_3", role.review_id],
    )

    assert [(e.preview.survivor_id, e.preview.absorbed_id) for e in approval.merges] == [
        (older_existing.survivor_id, older_existing.absorbed_id),
        ("cap_a2", "cap_s2"),
        ("cap_a3", "cap_s3"),
        (role.survivor_id, role.absorbed_id),
    ]


def test_a_batch_of_many_reviews_is_not_capped(cleanup_rig: CleanupRig) -> None:
    review_ids = [seed_capability_pair_with_review(cleanup_rig, n).review_id for n in range(20)]
    review_ids += [seed_role_pair_with_review(cleanup_rig, n).review_id for n in range(20)]
    store = FakeApprovalStore()

    approval = _create(cleanup_rig, store, review_ids)

    assert approval_row_count(store) == 1
    assert [entry.review_id for entry in approval.merges] == review_ids
