"""Near misses found by a restore are persisted with it, all or nothing.

The reviews exist after the restore, are listed, and resolve by keep-separate. A pair
that already has a review is not recorded again, nor on a second restore. A merged loser is
redirected, never reviewed. A restore that fails leaves no review.
The real `restore_instrument` runs through the gateway rig; the retry cases are
in `test_restore_concurrency.py`.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

import pytest
from company_merge._gateway_fakes import RigReadHandle

from ps_service.company_merge.graph_submission import GraphWriteTarget
from ps_service.company_merge.pending_review import list_pending_reviews, resolve_review
from ps_service.graph_gateway.errors import GraphWriteRejectedError
from ps_service.restore.errors import RestoreGroupRejectedError
from restore._near_miss_fixtures import (
    EXISTING,
    INCOMING,
    POLICY_SYSTEM,
    near_miss_artifact,
    reviews,
    seed_existing,
)

if TYPE_CHECKING:
    from ps_service.graph_gateway.gateway import GraphWriteGateway
    from restore._gateway_fakes import RestoreRig


def _read_handle(rig: RestoreRig) -> RigReadHandle:
    """A fresh read handle: a restore is applied by swap, so an older one reads a stale graph."""
    return RigReadHandle(rig.graph(POLICY_SYSTEM))


def _target(rig: RestoreRig) -> GraphWriteTarget:
    event_id = str(uuid.uuid4())
    rig.store.audit_rows.add(event_id)
    return GraphWriteTarget(rig.gateway, POLICY_SYSTEM, event_id)


def test_AC_BI_001_the_reviews_are_written_in_the_restores_own_group(
    restore_rig: RestoreRig,
) -> None:
    seed_existing(restore_rig)

    outcome = restore_rig.restore(near_miss_artifact())

    found = reviews(restore_rig)
    assert outcome.near_miss_reviews_created == len(INCOMING) == len(found)
    for review in found.values():
        incoming_id = review["incoming_id"]
        _, name, nearest = INCOMING[incoming_id]  # pyright: ignore[reportArgumentType]
        assert review["kind"] == "Capability"
        assert review["nearest_existing_id"] == nearest
        assert review["incoming_text"] == name
        assert review["nearest_existing_text"] == EXISTING[nearest][1]
        assert review["similarity"] == pytest.approx(0.8)
    # one transaction: the reviews sit in the same policy_system group as the restored content
    groups = restore_rig.store.read_groups_by_audit_event(restore_rig.audit_event_id)
    (leg,) = (g for g in groups if g.graph == POLICY_SYSTEM)
    ops = {(e.name, e.content.get("op")) for e in leg.entries}
    assert ("PendingReview", "upsert_node") in ops
    assert ("Capability", "upsert_node") in ops
    assert len(restore_rig.store.multi_appends) == 1


def test_AC_BI_002_the_reviews_are_listed_and_resolve_by_keep_separate(
    restore_rig: RestoreRig,
) -> None:
    seed_existing(restore_rig)
    restore_rig.restore(near_miss_artifact())

    listed = list_pending_reviews(_read_handle(restore_rig))

    assert {r.incoming_id for r in listed} == set(INCOMING)
    assert {r.kind for r in listed} == {"Capability"}
    outcome = resolve_review(
        _read_handle(restore_rig), listed[0].id, "keep-separate", _target(restore_rig)
    )
    assert outcome is not None
    remaining = list_pending_reviews(_read_handle(restore_rig))
    assert [r.id for r in remaining] == [listed[1].id]
    assert ("Capability", listed[0].incoming_id) in restore_rig.graph(POLICY_SYSTEM).nodes


def test_AC_BI_004_a_pair_that_already_has_a_review_is_not_recorded_again(
    restore_rig: RestoreRig,
) -> None:
    seed_existing(restore_rig)
    restore_rig.policy_system.nodes[("PendingReview", "review_earlier")] = {
        "id": "review_earlier",
        "kind": "Capability",
        "status": "pending",
        "incoming_id": "cap_in_a",
        "incoming_text": "Retain Records",
        "nearest_existing_id": "cap_ex_a",
        "nearest_existing_text": "Keep Records",
        "similarity": 0.8,
        "created_at": "2026-01-01T00:00:00+00:00",
    }

    outcome = restore_rig.restore(near_miss_artifact())

    assert outcome.near_miss_reviews_created == 1  # only the other pair
    assert {r["incoming_id"] for r in reviews(restore_rig).values()} == set(INCOMING)
    assert len(reviews(restore_rig)) == len(INCOMING)  # the earlier one plus one new


def test_AC_BI_005_restoring_twice_creates_no_second_set_of_reviews(
    restore_rig: RestoreRig,
) -> None:
    seed_existing(restore_rig)
    restore_rig.restore(near_miss_artifact())
    before = set(reviews(restore_rig))

    second = restore_rig.restore(near_miss_artifact())

    assert second.near_miss_reviews_created == 0
    assert set(reviews(restore_rig)) == before


def test_AC_BI_007_a_refused_group_leaves_no_review(restore_rig: RestoreRig) -> None:
    seed_existing(restore_rig)

    class RefusingGateway:
        """The rig's gateway, except every multi-graph group is refused."""

        def __init__(self, inner: GraphWriteGateway) -> None:
            self.submit_group = inner.submit_group
            self.read_log_position = inner.read_log_position
            self.is_caught_up = inner.is_caught_up
            self.catch_up = inner.catch_up

        def submit_multi_graph_group(self, group: object) -> object:
            del group
            raise GraphWriteRejectedError("the group is refused")

    refusing = RefusingGateway(restore_rig.gateway)
    restore_rig.open_gateway = lambda: refusing  # type: ignore[method-assign]

    with pytest.raises(RestoreGroupRejectedError):
        restore_rig.restore(near_miss_artifact())

    assert reviews(restore_rig) == {}
    assert restore_rig.store.groups == []
