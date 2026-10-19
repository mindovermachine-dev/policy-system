"""Tests for `ps_service.company_merge.pending_review.resolve_review`.

`resolve_review` only keeps a pair separate: it deletes the `PendingReview` record and nothing
else, and a missing review makes no graph change at all. A merge is not decided here; it is
requested through the graph-cleanup `merge-capabilities` approval, so `decision="merge"` is
refused before anything is read or written. A structured log entry records the decision and the
review id.

The write is ONE gateway group on `policy_system` (read, then delete), so these tests run
`resolve_review` over the gateway rig: the reads are answered from the rig's own graph and the
writes are asserted as gateway entries, not Cypher text. The keep-separate delete is also compared
with the frozen reference statement, modulo edge identity.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Any, cast

import pytest

from company_merge._gateway_fakes import POLICY_SYSTEM, RigReadHandle, UnusedSubmitter
from company_merge._legacy_reference import (
    FROZEN_STATEMENT_HASHES,
    LegacyRecordingGraph,
    write_legacy_resolve_keep_separate,
)
from ps_service.company_merge.errors import (
    CompanyMergePersistenceError,
    CompanyMergeValidationError,
)
from ps_service.company_merge.graph_submission import GraphWriteTarget
from ps_service.company_merge.models import ResolveOutcome
from ps_service.company_merge.pending_review import resolve_review
from ps_service.graph_gateway.digest import canonical_digest

if TYPE_CHECKING:
    from company_merge._fakes import MakeEmitter, ReadLines
    from company_merge._gateway_fakes import MergeRig

_REVIEW_ID = "review_aaa"


def _review_properties() -> dict[str, object]:
    return {
        "kind": "Capability",
        "status": "pending",
        "incoming_id": "cap_incoming",
        "incoming_text": "incoming near-miss text",
        "nearest_existing_id": "cap_existing",
        "nearest_existing_text": "existing near-miss text",
        "similarity": 0.91,
        "created_at": "2024-06-01T00:00:00+00:00",
    }


def _seed_review(rig: MergeRig) -> None:
    """Both candidates, an Obligation requiring the incoming one, and the review naming them."""
    graph = rig.policy_system
    graph.seed_node(("Capability",), "cap_incoming", {"name": "incoming"})
    graph.seed_node(("Capability",), "cap_existing", {"name": "existing"})
    graph.seed_node(("Obligation",), "obl_a", {"text": "text"})
    graph.seed_node(("PendingReview",), _REVIEW_ID, _review_properties())
    graph.seed_legacy_edge(("REQUIRES", "Obligation", "obl_a", "Capability", "cap_incoming"), {})


def _target(rig: MergeRig) -> GraphWriteTarget:
    return GraphWriteTarget(rig.gateway, POLICY_SYSTEM, rig.audit_event_id)


def test_resolve_keep_separate_deletes_review_only(
    make_emitter: MakeEmitter, merge_rig: MergeRig
) -> None:
    """The PendingReview record is removed, no other graph node/edge changes."""
    emitter, _log_path = make_emitter()
    _seed_review(merge_rig)
    candidates_before = {
        key: dict(props)
        for key, props in merge_rig.policy_system.nodes.items()
        if key[0] != "PendingReview"
    }

    outcome = resolve_review(
        merge_rig.legacy, _REVIEW_ID, "keep-separate", target=_target(merge_rig), emitter=emitter
    )

    assert outcome == ResolveOutcome(
        review_id="review_aaa", decision="keep-separate", winner_id=None, loser_id=None
    )
    # The review is read first (a plain read), then deleted through the gateway.
    assert "MATCH (r:PendingReview {id: $review_id}) RETURN" in merge_rig.legacy.statements[0]
    assert merge_rig.legacy.params_seen[0] == {"review_id": "review_aaa"}
    assert len(merge_rig.legacy.statements) == 1
    assert ("PendingReview", _REVIEW_ID) not in merge_rig.policy_system.nodes
    assert dict(merge_rig.policy_system.nodes) == candidates_before
    assert merge_rig.legacy.edge_count("REQUIRES") == 1


def test_keep_separate_submits_one_group_deleting_only_the_review_and_leaves_both_candidates_untouched(  # noqa: E501 -- name fixed by the plan
    make_emitter: MakeEmitter, merge_rig: MergeRig
) -> None:
    """The write is ONE gateway group whose only entry removes the review, under the audit event."""
    emitter, _log_path = make_emitter()
    _seed_review(merge_rig)

    resolve_review(
        merge_rig.legacy, _REVIEW_ID, "keep-separate", target=_target(merge_rig), emitter=emitter
    )

    review_groups = [g for g in merge_rig.groups if g.audit_event_id == merge_rig.audit_event_id]
    deletions = [
        (entry.name, entry.identity)
        for group in review_groups
        for entry in group.entries
        if entry.content.get("op") == "delete_node"
    ]
    assert deletions == [("PendingReview", _REVIEW_ID)]
    # Everything else that reached the log is the one-off identity stamp of the seeded legacy edge.
    assert [entry.content.get("op") for group in review_groups for entry in group.entries] == [
        "stamp_edge_identity",
        "delete_node",
    ]
    assert set(merge_rig.policy_system.nodes) == {
        ("Capability", "cap_incoming"),
        ("Capability", "cap_existing"),
        ("Obligation", "obl_a"),
    }


def test_resolve_nonexistent_id_returns_none_before_any_write(
    merge_rig: MergeRig,
) -> None:
    """An unknown/already-resolved id makes no graph changes."""
    target = GraphWriteTarget(UnusedSubmitter(), POLICY_SYSTEM, merge_rig.audit_event_id)

    outcome = resolve_review(merge_rig.legacy, "review_missing", "keep-separate", target=target)

    assert outcome is None
    assert len(merge_rig.legacy.statements) == 1
    assert "MATCH (r:PendingReview {id: $review_id}) RETURN" in merge_rig.legacy.statements[0]
    assert merge_rig.legacy.params_seen[0] == {"review_id": "review_missing"}
    assert merge_rig.logged() == []


def test_resolve_keep_separate_emits_structured_log_entry(
    make_emitter: MakeEmitter, read_lines: ReadLines, merge_rig: MergeRig
) -> None:
    """Keep-separate logs decision, review id, and timestamp are logged."""
    emitter, log_path = make_emitter()
    _seed_review(merge_rig)

    resolve_review(
        merge_rig.legacy, _REVIEW_ID, "keep-separate", target=_target(merge_rig), emitter=emitter
    )
    emitter.flush()

    entries = read_lines(log_path)
    assert len(entries) == 1
    entry = entries[0]
    assert entry["component"] == "company_merge"
    assert entry["action"] == "resolve_near_miss_review"
    assert entry["entity_id"] == "review_aaa"
    assert entry["outcome"] == "keep-separate"
    assert "timestamp" in entry


def test_resolve_nonexistent_id_emits_no_log_entry(
    make_emitter: MakeEmitter, read_lines: ReadLines, merge_rig: MergeRig
) -> None:
    """No log noise for an id that was never resolved -- nothing happened to log."""
    emitter, log_path = make_emitter()
    target = GraphWriteTarget(UnusedSubmitter(), POLICY_SYSTEM, merge_rig.audit_event_id)

    resolve_review(
        merge_rig.legacy, "review_missing", "keep-separate", target=target, emitter=emitter
    )
    emitter.flush()

    assert read_lines(log_path) == []


def test_keep_separate_of_a_review_deleted_since_the_read_is_a_no_op_that_still_resolves(
    make_emitter: MakeEmitter, merge_rig: MergeRig
) -> None:
    """The read-then-delete has no lock: a review gone in between logs nothing.

    The gateway drops a delete of an absent node (`unchanged`) and nothing is logged for it.
    """
    emitter, _log_path = make_emitter()
    _seed_review(merge_rig)

    class _VanishingReview(RigReadHandle):
        def _read(self, q: str, params: dict[str, object] | None) -> list[object]:
            rows = super()._read(q, params)
            self._graph.nodes.pop(("PendingReview", _REVIEW_ID), None)  # deleted after the read
            return rows

    vanishing = _VanishingReview(merge_rig.policy_system)

    outcome = resolve_review(
        vanishing, _REVIEW_ID, "keep-separate", target=_target(merge_rig), emitter=emitter
    )

    assert outcome == ResolveOutcome(review_id=_REVIEW_ID, decision="keep-separate")
    assert [
        e for g in merge_rig.groups for e in g.entries if e.content.get("op") == "delete_node"
    ] == []


def test_a_refused_keep_separate_group_raises_the_persistence_error_and_keeps_the_review(
    make_emitter: MakeEmitter, read_lines: ReadLines, merge_rig: MergeRig
) -> None:
    """A gateway that cannot log the group fails the resolution, nothing is deleted."""
    emitter, log_path = make_emitter()
    _seed_review(merge_rig)
    merge_rig.store.fail_appends()

    with pytest.raises(CompanyMergePersistenceError, match=r"^FalkorDB write failed: "):
        resolve_review(
            merge_rig.legacy,
            _REVIEW_ID,
            "keep-separate",
            target=_target(merge_rig),
            emitter=emitter,
        )
    emitter.flush()

    assert ("PendingReview", _REVIEW_ID) in merge_rig.policy_system.nodes
    assert read_lines(log_path) == []


def test_the_delete_statement_the_reference_replays_is_the_one_pinned_at_freeze() -> None:
    """The frozen keep-separate statement is the original `_DELETE_REVIEW_QUERY`, byte for byte."""
    recorded: list[str] = []

    class _Recorder:
        def query(self, q: str, params: dict[str, object] | None = None) -> object:
            del params
            recorded.append(q)

    write_legacy_resolve_keep_separate(_Recorder(), _REVIEW_ID)

    assert [hashlib.sha256(q.encode()).hexdigest() for q in recorded] == [
        FROZEN_STATEMENT_HASHES["delete_review"]
    ]


def test_keep_separate_leaves_the_graph_the_frozen_reference_leaves(
    make_emitter: MakeEmitter, merge_rig: MergeRig
) -> None:
    """Digest of the gateway path equals the frozen `DELETE r`'s, modulo edge
    identity.
    """
    emitter, _log_path = make_emitter()
    _seed_review(merge_rig)
    reference = LegacyRecordingGraph()
    for (label, node_id), props in merge_rig.policy_system.nodes.items():
        reference.nodes[(label, node_id)] = dict(props)
    reference.edges[("REQUIRES", "Obligation", "obl_a", "Capability", "cap_incoming")] = {}
    write_legacy_resolve_keep_separate(reference, _REVIEW_ID)

    resolve_review(
        merge_rig.legacy, _REVIEW_ID, "keep-separate", target=_target(merge_rig), emitter=emitter
    )

    assert canonical_digest(merge_rig.policy_system) == canonical_digest(
        reference.as_gateway_graph()
    )


def test_a_merge_decision_is_refused_before_anything_is_read_or_written(
    merge_rig: MergeRig,
) -> None:
    """A merge goes through the cleanup `merge-capabilities` approval, never through this module."""
    _seed_review(merge_rig)
    before = {key: dict(props) for key, props in merge_rig.policy_system.nodes.items()}
    target = GraphWriteTarget(UnusedSubmitter(), POLICY_SYSTEM, merge_rig.audit_event_id)

    with pytest.raises(CompanyMergeValidationError, match="merge"):
        resolve_review(merge_rig.legacy, _REVIEW_ID, cast("Any", "merge"), target)

    assert merge_rig.legacy.statements == []
    assert merge_rig.policy_system.nodes == before
    assert merge_rig.logged() == []
