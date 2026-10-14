"""Tests for `ps_service.company_merge.pending_review.persist_pending_reviews`.

A `PendingReview` node is persisted for every
`NearMissPair` surfaced during a Company Merge dedup pass, carrying the pair's incoming id/text,
nearest-existing id/text and similarity score verbatim, so a later review workflow never needs to
re-query embeddings to render or act on it.

Each review is an `UpsertNode` in ONE gateway group on
`policy_system`. Its uuid and `created_at` are drawn from the injected `ValueSource` while the group
is BUILT, so the group carries them: submitting it twice leaves exactly one node per review, and a
replay of the log reproduces the same ids and timestamps.
"""

from __future__ import annotations

import hashlib
import math
from datetime import datetime
from typing import TYPE_CHECKING

import pytest
import redis.exceptions

from company_merge._gateway_fakes import POLICY_SYSTEM, stepped_values
from company_merge._legacy_reference import (
    FROZEN_STATEMENT_HASHES,
    LegacyRecordingGraph,
    write_legacy_pending_reviews,
)
from ps_service.company_merge.errors import CompanyMergePersistenceError
from ps_service.company_merge.graph_submission import GraphWriteTarget
from ps_service.company_merge.models import NearMissPair
from ps_service.company_merge.pending_review import (
    build_pending_review_primitives,
    persist_pending_reviews,
)
from ps_service.graph_gateway.digest import canonical_digest
from ps_service.graph_gateway.label_allow_list import ALLOWED_NODE_LABELS
from ps_service.graph_gateway.models import MutationGroup

if TYPE_CHECKING:
    from company_merge._gateway_fakes import MergeRig


def _near_miss(
    incoming_id: str = "capability_incoming_abc",
    nearest_existing_id: str = "capability_existing_xyz",
    similarity: float = 0.62,
) -> NearMissPair:
    return NearMissPair(
        incoming_id=incoming_id,
        incoming_text="Report the incident to the authority.",
        nearest_existing_id=nearest_existing_id,
        nearest_existing_text="Conduct a risk assessment.",
        similarity=similarity,
    )


def _target(rig: MergeRig) -> GraphWriteTarget:
    return GraphWriteTarget(rig.gateway, POLICY_SYSTEM, rig.audit_event_id)


def _reviews(rig: MergeRig) -> dict[str, dict[str, object]]:
    return {
        node_id: props
        for (label, node_id), props in rig.policy_system.nodes.items()
        if label == "PendingReview"
    }


def test_persist_pending_reviews_creates_node_with_pair_fields_verbatim(
    merge_rig: MergeRig,
) -> None:
    pair = _near_miss()

    persist_pending_reviews(_target(merge_rig), (pair,), kind="Capability")

    [(review_id, props)] = _reviews(merge_rig).items()
    assert review_id.startswith("review_")
    assert props["kind"] == "Capability"
    assert props["status"] == "pending"
    assert props["incoming_id"] == pair.incoming_id
    assert props["incoming_text"] == pair.incoming_text
    assert props["nearest_existing_id"] == pair.nearest_existing_id
    assert props["nearest_existing_text"] == pair.nearest_existing_text
    assert props["similarity"] == pair.similarity
    created_at = props["created_at"]
    assert isinstance(created_at, str)
    datetime.fromisoformat(created_at)  # round-trips without raising


def test_persist_pending_reviews_no_near_misses_submits_no_group(merge_rig: MergeRig) -> None:
    persist_pending_reviews(_target(merge_rig), (), kind="Capability")

    assert merge_rig.groups == []


def test_persist_pending_reviews_is_one_group_with_one_node_per_pair_and_distinct_ids(
    merge_rig: MergeRig,
) -> None:
    pair_a = _near_miss(
        incoming_id="capability_incoming_a", nearest_existing_id="capability_existing_a"
    )
    pair_b = _near_miss(
        incoming_id="capability_incoming_b", nearest_existing_id="capability_existing_b"
    )

    persist_pending_reviews(_target(merge_rig), (pair_a, pair_b), kind="Policy")

    assert len(merge_rig.groups) == 1
    group = merge_rig.groups[0]
    assert group.audit_event_id == merge_rig.audit_event_id
    assert [e.name for e in group.entries] == ["PendingReview", "PendingReview"]
    assert len({e.identity for e in group.entries}) == 2
    assert all(props["kind"] == "Policy" for props in _reviews(merge_rig).values())


def test_each_near_miss_becomes_a_pending_review_node_with_uuid_id_and_created_at_fixed_before_submission(  # noqa: E501 -- name fixed by the plan
    merge_rig: MergeRig,
) -> None:
    """The ids and instants come from the injected source, one pair at a time, id first."""
    values = stepped_values()
    pairs = (_near_miss("capability_a", "capability_x"), _near_miss("capability_b", "capability_y"))

    persist_pending_reviews(_target(merge_rig), pairs, kind="Capability", values=values)

    reviews = _reviews(merge_rig)
    assert list(reviews) == ["review_fixed-id-1", "review_fixed-id-2"]
    assert [props["created_at"] for props in reviews.values()] == [
        "2026-03-01T12:00:00+00:00",
        "2026-03-01T12:00:01+00:00",
    ]
    assert values.new_id() == "fixed-id-3"  # exactly one id per pair was drawn


def test_applying_the_review_group_twice_leaves_exactly_one_node_per_review(
    merge_rig: MergeRig,
) -> None:
    pairs = (_near_miss("capability_a", "capability_x"), _near_miss("capability_b", "capability_y"))
    primitives = build_pending_review_primitives(pairs, kind="Capability", values=stepped_values())
    group = MutationGroup(
        graph=POLICY_SYSTEM, audit_event_id=merge_rig.audit_event_id, primitives=primitives
    )

    first = merge_rig.gateway.submit_group(group)
    second = merge_rig.gateway.submit_group(group)

    assert (first.status, second.status) == ("applied", "unchanged")
    assert list(_reviews(merge_rig)) == ["review_fixed-id-1", "review_fixed-id-2"]
    assert len(merge_rig.logged()) == 2


def test_a_replay_reproduces_the_review_ids_and_created_at(merge_rig: MergeRig) -> None:
    persist_pending_reviews(
        _target(merge_rig), (_near_miss(),), kind="Capability", values=stepped_values()
    )
    before = canonical_digest(merge_rig.policy_system)
    logged = merge_rig.logged()[0]

    merge_rig.gateway.replay_graph(POLICY_SYSTEM)

    assert canonical_digest(merge_rig.policy_system) == before
    review = _reviews(merge_rig)["review_fixed-id-1"]
    assert review["created_at"] == logged.content["properties"]["created_at"]  # pyright: ignore[reportIndexIssue, reportArgumentType]
    assert review["created_at"] == "2026-03-01T12:00:00+00:00"


def test_a_non_finite_similarity_raises_the_persistence_error_before_submit(
    merge_rig: MergeRig,
) -> None:
    with pytest.raises(CompanyMergePersistenceError, match=r"PendingReview.*cannot store"):
        persist_pending_reviews(
            _target(merge_rig), (_near_miss(similarity=math.nan),), kind="Capability"
        )

    assert merge_rig.groups == []


def test_a_group_the_log_cannot_take_raises_the_sanitized_persistence_error(
    merge_rig: MergeRig,
) -> None:
    merge_rig.store.fail_appends()

    with pytest.raises(CompanyMergePersistenceError, match=r"^FalkorDB write failed: ") as excinfo:
        persist_pending_reviews(_target(merge_rig), (_near_miss(),), kind="Capability")

    assert "Postgres" not in str(excinfo.value)
    assert not isinstance(excinfo.value.__cause__, redis.exceptions.RedisError)


def test_the_review_label_is_gateway_allow_listed() -> None:
    assert "PendingReview" in ALLOWED_NODE_LABELS


def test_the_frozen_pending_review_statement_matches_its_pinned_hash() -> None:
    class _Capture:
        def __init__(self) -> None:
            self.statements: list[str] = []

        def query(self, q: str, params: dict[str, object] | None = None) -> object:
            del params
            self.statements.append(q)

    capture = _Capture()

    write_legacy_pending_reviews(
        capture, (_near_miss(),), kind="Capability", values=stepped_values()
    )

    assert [hashlib.sha256(s.encode()).hexdigest() for s in capture.statements] == [
        FROZEN_STATEMENT_HASHES["pending_review"]
    ]


def test_the_frozen_writer_and_the_gateway_path_build_the_same_review_nodes(
    merge_rig: MergeRig,
) -> None:
    """Family F (PendingReview), same generated values; modulo edge identity (none)."""
    pairs = (_near_miss("capability_a", "capability_x"), _near_miss("capability_b", "capability_y"))
    reference = LegacyRecordingGraph()
    write_legacy_pending_reviews(reference, pairs, kind="Policy", values=stepped_values())

    persist_pending_reviews(_target(merge_rig), pairs, kind="Policy", values=stepped_values())

    assert canonical_digest(merge_rig.policy_system) == canonical_digest(
        reference.as_gateway_graph()
    )
    assert len(reference.nodes) == 2
