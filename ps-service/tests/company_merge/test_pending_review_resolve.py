"""Tests for `ps_service.company_merge.pending_review.resolve_review`
(issue #35): AC-BI-004 (keep-separate deletes only the `PendingReview`
record), AC-BI-005/006/007 (merge re-points edges, deletes the loser and the
review, atomically, with deterministic winner selection), AC-BI-008 (both
decisions' not-found paths make no graph changes at all -- including merge's
own "stale reference" case, CHANGES.md H2), and AC-BI-009 (a structured log
entry records the decision, review id, and timestamp -- plus winner/loser
for merge).

Issue #210: both decisions write ONE gateway group on `policy_system` (read, resolve the winner in
Python, write), so these tests run `resolve_review` over the gateway rig: the reads are answered
from the rig's own graph and the writes are asserted as gateway entries, not Cypher text. The
winner rule and the five rewired families are also compared with the frozen pre-#210 statement
(`_legacy_resolve_reference`), modulo edge identity; the `falkordb_live` test at the bottom is the
live anchor and is not runnable in the implementation sandbox.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, cast

import pytest

from company_merge._gateway_fakes import POLICY_SYSTEM, RigReadHandle, UnusedSubmitter
from company_merge._legacy_reference import (
    FROZEN_STATEMENT_HASHES,
    LegacyRecordingGraph,
    write_legacy_resolve_keep_separate,
    write_legacy_resolve_merge,
)
from ps_service.company_merge.errors import CompanyMergePersistenceError, StalePendingReviewError
from ps_service.company_merge.falkordb_client import connect_from_config, select_graph
from ps_service.company_merge.graph_submission import GraphWriteTarget
from ps_service.company_merge.models import ResolveOutcome
from ps_service.company_merge.pending_review import resolve_review
from ps_service.config import load_config
from ps_service.graph_gateway.default_gateway import build_default_graph_write_gateway
from ps_service.graph_gateway.digest import canonical_digest
from ps_service.graph_gateway.system_audit import build_system_audit_recorder

if TYPE_CHECKING:
    from collections.abc import Callable

    from graph_gateway._fakes import InMemoryGraph

    from company_merge._fakes import MakeEmitter, ReadLines
    from company_merge._gateway_fakes import MergeRig
    from ps_service.graph_gateway.models import GraphLogEntry
    from ps_service.logging import LogEmitter

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
    """AC-BI-004: the PendingReview record is removed, no other graph node/edge changes."""
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
    """AC-BI-008 (not-found half): an unknown/already-resolved id makes no graph changes."""
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
    """AC-BI-009 (keep-separate half): decision, review id, and timestamp are logged."""
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
    """The read-then-delete has no lock (C-0): a review gone in between logs nothing, as before.

    The old `MATCH ... DELETE r` matched nothing and succeeded; the gateway drops a delete of an
    absent node (`unchanged`) and nothing is logged for it.
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
    """AC-BI-008: a gateway that cannot log the group fails the resolution, nothing is deleted."""
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
    """R-keep (AC-BI-002): digest of the gateway path equals the frozen `DELETE r`'s, modulo edge
    identity. Met modulo identity; live digest and live interpreter agreement are not verifiable
    here (no FalkorDB in this sandbox).
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


# --- decision="merge": read, resolve the winner, write one group (issue #35 slice 4; #210 10b) ---

_OLD = "2020-01-01T00:00:00+00:00"
_NEW = "2024-01-01T00:00:00+00:00"
_INCOMING = "cap_incoming"
_EXISTING = "cap_existing"
type _Edge = tuple[str, str, str, str, str]
type _Node = tuple[str, str, dict[str, object]]


def _seed_merge(
    rig: MergeRig,
    *,
    incoming_created: str | None = _NEW,
    existing_created: str | None = _OLD,
    kind: str = "Capability",
    nodes: tuple[_Node, ...] = (),
    edges: tuple[_Edge, ...] = (),
) -> None:
    """Both candidates (with their `created_at`), the review naming them, extra nodes and edges.

    Edges are seeded as a pre-gateway writer left them (no identity), so the first group stamps.
    """
    graph = rig.policy_system
    for node_id, created in ((_INCOMING, incoming_created), (_EXISTING, existing_created)):
        properties: dict[str, object] = {"name": node_id}
        if created is not None:
            properties["created_at"] = created
        graph.seed_node((kind,), node_id, properties)
    graph.seed_node(("PendingReview",), _REVIEW_ID, {**_review_properties(), "kind": kind})
    for label, node_id, properties in nodes:
        graph.seed_node((label,), node_id, properties)
    for edge in edges:
        graph.seed_legacy_edge(edge, {})


def _merge(rig: MergeRig, emitter: LogEmitter) -> ResolveOutcome | None:
    return resolve_review(rig.legacy, _REVIEW_ID, "merge", _target(rig), emitter=emitter)


def _resolution_entries(rig: MergeRig) -> list[GraphLogEntry]:
    """The entries of the resolution's groups, in order, without the one-off identity stamp."""
    return [
        entry
        for group in rig.groups
        if group.audit_event_id == rig.audit_event_id
        for entry in group.entries
        if entry.content.get("op") != "stamp_edge_identity"
    ]


_CAPABILITY_LOSER_NODES: tuple[_Node, ...] = (
    ("Obligation", "obl_a", {"text": "t"}),
    ("Obligation", "obl_b", {"text": "u"}),
    ("Policy", "pol_a", {"name": "a"}),
    ("Capability", "cap_tomb", {"name": "tomb", "status": "merged"}),
)
_CAPABILITY_LOSER_EDGES: tuple[_Edge, ...] = (
    ("REQUIRES", "Obligation", "obl_b", "Capability", _INCOMING),
    ("REQUIRES", "Obligation", "obl_a", "Capability", _INCOMING),
    ("GOVERNED_BY", "Capability", _INCOMING, "Policy", "pol_a"),
    ("MERGED_INTO", "Capability", "cap_tomb", "Capability", _INCOMING),
)
_POLICY_LOSER_NODES: tuple[_Node, ...] = (
    ("Capability", "cap_a", {"name": "a"}),
    ("Standard", "std_a", {"name": "s"}),
)
_POLICY_LOSER_EDGES: tuple[_Edge, ...] = (
    ("GOVERNED_BY", "Capability", "cap_a", "Policy", _INCOMING),
    ("SUPPORTED_BY", "Policy", _INCOMING, "Standard", "std_a"),
)


def test_older_created_at_wins_and_the_loser_is_the_newer_node(
    make_emitter: MakeEmitter, merge_rig: MergeRig
) -> None:
    """AC-BI-003: the incoming node is newer, so the existing one wins; the loser is gone."""
    emitter, _log_path = make_emitter()
    _seed_merge(merge_rig)

    outcome = _merge(merge_rig, emitter)

    assert outcome == ResolveOutcome(
        review_id=_REVIEW_ID, decision="merge", winner_id=_EXISTING, loser_id=_INCOMING
    )
    assert ("Capability", _INCOMING) not in merge_rig.policy_system.nodes
    assert ("Capability", _EXISTING) in merge_rig.policy_system.nodes
    assert ("PendingReview", _REVIEW_ID) not in merge_rig.policy_system.nodes


def test_a_capability_losers_families_are_rewired_in_one_group_before_the_deletes(
    make_emitter: MakeEmitter, merge_rig: MergeRig
) -> None:
    """Inbound REQUIRES (twice: C1 row multiplication), outbound GOVERNED_BY and inbound
    MERGED_INTO move to the winner: ONE group of `UpsertEdge`s (one per edge, in the statement's
    block order), then `DeleteNode(loser)`, then `DeleteNode(PendingReview)`.
    """
    emitter, _log_path = make_emitter()
    _seed_merge(merge_rig, nodes=_CAPABILITY_LOSER_NODES, edges=_CAPABILITY_LOSER_EDGES)

    _merge(merge_rig, emitter)

    entries = _resolution_entries(merge_rig)
    assert [(e.content.get("op"), e.name, e.identity) for e in entries] == [
        ("upsert_edge", "REQUIRES", f"REQUIRES:obl_a->{_EXISTING}"),
        ("upsert_edge", "REQUIRES", f"REQUIRES:obl_b->{_EXISTING}"),
        ("upsert_edge", "GOVERNED_BY", f"GOVERNED_BY:{_EXISTING}->pol_a"),
        ("upsert_edge", "MERGED_INTO", f"MERGED_INTO:cap_tomb->{_EXISTING}"),
        ("delete_node", "Capability", _INCOMING),
        ("delete_node", "PendingReview", _REVIEW_ID),
    ]
    assert len({e.group_id for e in entries}) == 1
    assert merge_rig.legacy.edge_count("REQUIRES") == 2


def test_a_policy_losers_families_are_rewired_in_one_group_before_the_deletes(
    make_emitter: MakeEmitter, merge_rig: MergeRig
) -> None:
    """Inbound GOVERNED_BY and outbound SUPPORTED_BY: the same rule holds for kind=Policy."""
    emitter, _log_path = make_emitter()
    _seed_merge(merge_rig, kind="Policy", nodes=_POLICY_LOSER_NODES, edges=_POLICY_LOSER_EDGES)

    outcome = _merge(merge_rig, emitter)

    assert outcome is not None
    assert (outcome.winner_id, outcome.loser_id) == (_EXISTING, _INCOMING)
    entries = _resolution_entries(merge_rig)
    assert [(e.content.get("op"), e.name, e.identity) for e in entries] == [
        ("upsert_edge", "GOVERNED_BY", f"GOVERNED_BY:cap_a->{_EXISTING}"),
        ("upsert_edge", "SUPPORTED_BY", f"SUPPORTED_BY:{_EXISTING}->std_a"),
        ("delete_node", "Policy", _INCOMING),
        ("delete_node", "PendingReview", _REVIEW_ID),
    ]


@pytest.mark.parametrize(
    ("kind", "nodes", "edges"),
    [
        pytest.param("Capability", _CAPABILITY_LOSER_NODES, _CAPABILITY_LOSER_EDGES, id="R-older"),
        pytest.param("Policy", _POLICY_LOSER_NODES, _POLICY_LOSER_EDGES, id="R-five-policy"),
    ],
)
def test_the_merge_leaves_the_graph_the_frozen_reference_leaves(
    kind: str,
    nodes: tuple[_Node, ...],
    edges: tuple[_Edge, ...],
    make_emitter: MakeEmitter,
    merge_rig: MergeRig,
) -> None:
    """R-older / R-five (AC-BI-002): digest of the gateway path equals the frozen statement's,
    modulo edge identity. Met modulo identity; live digest and live interpreter agreement are not
    verifiable here (no FalkorDB in this sandbox).
    """
    emitter, _log_path = make_emitter()
    _seed_merge(merge_rig, kind=kind, nodes=nodes, edges=edges)
    reference = LegacyRecordingGraph.copy_of(merge_rig.policy_system)
    expected = write_legacy_resolve_merge(reference, _REVIEW_ID)

    outcome = _merge(merge_rig, emitter)

    assert outcome is not None
    assert expected == (outcome.winner_id, outcome.loser_id)
    assert canonical_digest(merge_rig.policy_system) == canonical_digest(
        reference.as_gateway_graph()
    )


_WINNER_RULE_CASES = [
    pytest.param(_OLD, _NEW, _INCOMING, id="R-incoming-older"),
    pytest.param(_NEW, _OLD, _EXISTING, id="R-existing-older"),
    pytest.param(_OLD, _OLD, _INCOMING, id="R-tie"),
    pytest.param(None, _OLD, _INCOMING, id="R-incoming-missing"),
    pytest.param(_OLD, None, _EXISTING, id="R-existing-missing"),
    pytest.param(None, None, _INCOMING, id="R-both-missing"),
]


@pytest.mark.parametrize(
    ("kind", "nodes", "edges"),
    [
        pytest.param(
            "Capability", _CAPABILITY_LOSER_NODES, _CAPABILITY_LOSER_EDGES, id="capability"
        ),
        pytest.param("Policy", _POLICY_LOSER_NODES, _POLICY_LOSER_EDGES, id="policy"),
    ],
)
@pytest.mark.parametrize(("incoming_created", "existing_created", "winner"), _WINNER_RULE_CASES)
def test_the_winner_rule_leaves_the_graph_the_frozen_reference_leaves(
    incoming_created: str | None,
    existing_created: str | None,
    winner: str,
    kind: str,
    nodes: tuple[_Node, ...],
    edges: tuple[_Edge, ...],
    make_emitter: MakeEmitter,
    merge_rig: MergeRig,
) -> None:
    """R-tie / R-missing / R-both-missing, both kinds (AC-BI-002, AC-BI-003): the older node wins,
    a missing `created_at` wins, a tie (and both missing) goes to the incoming node, and the digest
    equals the frozen statement's, modulo edge identity. The winner is asserted explicitly so the
    rule cannot drift together with the reference. Met modulo identity; live digest and live
    interpreter agreement are not verifiable here (no FalkorDB in this sandbox).
    """
    emitter, _log_path = make_emitter()
    _seed_merge(
        merge_rig,
        incoming_created=incoming_created,
        existing_created=existing_created,
        kind=kind,
        nodes=nodes,
        edges=edges,
    )
    reference = LegacyRecordingGraph.copy_of(merge_rig.policy_system)
    expected = write_legacy_resolve_merge(reference, _REVIEW_ID)

    outcome = _merge(merge_rig, emitter)

    loser = _EXISTING if winner == _INCOMING else _INCOMING
    assert outcome == ResolveOutcome(
        review_id=_REVIEW_ID, decision="merge", winner_id=winner, loser_id=loser
    )
    assert expected == (winner, loser)
    assert (kind, loser) not in merge_rig.policy_system.nodes
    assert canonical_digest(merge_rig.policy_system) == canonical_digest(
        reference.as_gateway_graph()
    )


def test_a_tie_that_went_to_the_other_node_would_not_match_the_reference(
    make_emitter: MakeEmitter, merge_rig: MergeRig
) -> None:
    """Guard of the guard: the tie scenario is discriminating. Resolving the same tie with the
    nodes swapped (existing as winner) gives a digest different from the reference's.
    """
    emitter, _log_path = make_emitter()
    _seed_merge(merge_rig, incoming_created=_OLD, existing_created=_OLD)
    reference = LegacyRecordingGraph.copy_of(merge_rig.policy_system)
    write_legacy_resolve_merge(reference, _REVIEW_ID)
    swapped = LegacyRecordingGraph.copy_of(merge_rig.policy_system)
    swapped.nodes.pop(("Capability", _INCOMING))
    swapped.nodes.pop(("PendingReview", _REVIEW_ID))

    _merge(merge_rig, emitter)

    assert canonical_digest(merge_rig.policy_system) == canonical_digest(
        reference.as_gateway_graph()
    )
    assert canonical_digest(merge_rig.policy_system) != canonical_digest(swapped.as_gateway_graph())


def test_an_edge_the_winner_already_has_is_not_logged_again(
    make_emitter: MakeEmitter, merge_rig: MergeRig
) -> None:
    """The rewire of an edge the winner already holds is dropped by the gateway (no-op)."""
    emitter, _log_path = make_emitter()
    _seed_merge(
        merge_rig,
        nodes=(("Obligation", "obl_a", {"text": "t"}),),
        edges=(
            ("REQUIRES", "Obligation", "obl_a", "Capability", _INCOMING),
            ("REQUIRES", "Obligation", "obl_a", "Capability", _EXISTING),
        ),
    )

    _merge(merge_rig, emitter)

    assert [(e.content.get("op"), e.name) for e in _resolution_entries(merge_rig)] == [
        ("delete_node", "Capability"),
        ("delete_node", "PendingReview"),
    ]
    assert merge_rig.legacy.edge_count("REQUIRES") == 1


def test_several_obligations_requiring_the_loser_leave_the_graph_the_frozen_reference_leaves(
    make_emitter: MakeEmitter, merge_rig: MergeRig
) -> None:
    """C1 row multiplication (the `WITH DISTINCT` comment of the retired statement): three
    Obligations require the loser, one of them already requires the winner and one Policy is
    reachable twice. Each edge ends up once on the winner; the digest equals the reference's,
    modulo edge identity (live digest not verifiable here).
    """
    emitter, _log_path = make_emitter()
    _seed_merge(
        merge_rig,
        nodes=(
            ("Obligation", "obl_a", {"text": "a"}),
            ("Obligation", "obl_b", {"text": "b"}),
            ("Obligation", "obl_c", {"text": "c"}),
            ("Policy", "pol_a", {"name": "a"}),
        ),
        edges=(
            ("REQUIRES", "Obligation", "obl_a", "Capability", _INCOMING),
            ("REQUIRES", "Obligation", "obl_b", "Capability", _INCOMING),
            ("REQUIRES", "Obligation", "obl_c", "Capability", _INCOMING),
            ("REQUIRES", "Obligation", "obl_c", "Capability", _EXISTING),
            ("GOVERNED_BY", "Capability", _INCOMING, "Policy", "pol_a"),
        ),
    )
    reference = LegacyRecordingGraph.copy_of(merge_rig.policy_system)
    write_legacy_resolve_merge(reference, _REVIEW_ID)

    _merge(merge_rig, emitter)

    assert [(e.content.get("op"), e.name) for e in _resolution_entries(merge_rig)] == [
        ("upsert_edge", "REQUIRES"),
        ("upsert_edge", "REQUIRES"),
        ("upsert_edge", "GOVERNED_BY"),
        ("delete_node", "Capability"),
        ("delete_node", "PendingReview"),
    ]
    assert merge_rig.legacy.edge_count("REQUIRES") == 3
    assert canonical_digest(merge_rig.policy_system) == canonical_digest(
        reference.as_gateway_graph()
    )


def test_a_tombstone_pointing_at_the_loser_ends_up_where_the_frozen_reference_puts_it(
    make_emitter: MakeEmitter, merge_rig: MergeRig
) -> None:
    """Issue #190: the inbound `MERGED_INTO` of a merged tombstone moves onto the winner; the
    digest equals the reference's, modulo edge identity (live digest not verifiable here).
    """
    emitter, _log_path = make_emitter()
    _seed_merge(
        merge_rig,
        nodes=(("Capability", "cap_tomb", {"name": "tomb", "status": "merged"}),),
        edges=(("MERGED_INTO", "Capability", "cap_tomb", "Capability", _INCOMING),),
    )
    reference = LegacyRecordingGraph.copy_of(merge_rig.policy_system)
    write_legacy_resolve_merge(reference, _REVIEW_ID)

    _merge(merge_rig, emitter)

    assert {key[:5] for key in merge_rig.policy_system.edges if key[0] == "MERGED_INTO"} == {
        ("MERGED_INTO", "Capability", "cap_tomb", "Capability", _EXISTING)
    }
    assert canonical_digest(merge_rig.policy_system) == canonical_digest(
        reference.as_gateway_graph()
    )


class _RaceAfterReads(RigReadHandle):
    """A read handle that runs `race` once, right after the last loser-edge read: the window
    between the reads and the single write, in which the old code held no lock either.
    """

    def __init__(self, graph: InMemoryGraph, race: Callable[[], object]) -> None:
        super().__init__(graph)
        self._race = race
        self._loser_reads = 0

    def _read(self, q: str, params: dict[str, object] | None) -> list[object]:
        rows = super()._read(q, params)
        if params is not None and "loser_id" in params:
            self._loser_reads += 1
            if self._loser_reads == 5:
                self._race()
        return rows


def test_a_review_resolved_elsewhere_after_the_reads_still_merges_and_its_delete_is_a_no_op(
    make_emitter: MakeEmitter, merge_rig: MergeRig
) -> None:
    """The review vanishes after the reads (e.g. resolved by another caller). The old statement
    rewired and then returned no row (IndexError); the group now rewires, deletes the loser and
    finds the review already gone: the outcome is reported and nothing raises (recorded in 10b).
    """
    emitter, _log_path = make_emitter()
    _seed_merge(merge_rig, nodes=_CAPABILITY_LOSER_NODES, edges=_CAPABILITY_LOSER_EDGES)
    graph = merge_rig.policy_system
    handle = _RaceAfterReads(graph, lambda: graph.nodes.pop(("PendingReview", _REVIEW_ID)))

    outcome = resolve_review(handle, _REVIEW_ID, "merge", _target(merge_rig), emitter=emitter)

    assert outcome is not None
    assert (outcome.winner_id, outcome.loser_id) == (_EXISTING, _INCOMING)
    assert ("Capability", _INCOMING) not in graph.nodes
    assert handle.edge_count("REQUIRES") == 2
    ops = [(e.content.get("op"), e.name) for e in _resolution_entries(merge_rig)]
    assert ops == [
        ("upsert_edge", "REQUIRES"),
        ("upsert_edge", "REQUIRES"),
        ("upsert_edge", "GOVERNED_BY"),
        ("upsert_edge", "MERGED_INTO"),
        ("delete_node", "Capability"),
    ]


def test_a_candidate_deleted_after_the_reads_is_a_refused_group_and_nothing_changes(
    make_emitter: MakeEmitter, merge_rig: MergeRig
) -> None:
    """AC-BI-008 / atomic: the winner vanishes between the reads and the write. The gateway
    refuses the group on the missing endpoint (`MissingTargetError`): the persistence error is
    raised, and neither the loser, the review nor any edge changed.
    """
    emitter, _log_path = make_emitter()
    _seed_merge(merge_rig, nodes=_CAPABILITY_LOSER_NODES, edges=_CAPABILITY_LOSER_EDGES)
    graph = merge_rig.policy_system
    handle = _RaceAfterReads(graph, lambda: graph.nodes.pop(("Capability", _EXISTING)))

    with pytest.raises(CompanyMergePersistenceError, match=r"^FalkorDB write failed: "):
        resolve_review(handle, _REVIEW_ID, "merge", _target(merge_rig), emitter=emitter)

    assert _resolution_entries(merge_rig) == []
    assert ("Capability", _INCOMING) in graph.nodes
    assert ("PendingReview", _REVIEW_ID) in graph.nodes
    assert handle.edge_count("REQUIRES") == 2


def test_a_refused_merge_group_raises_the_persistence_error_and_leaves_everything_in_place(
    make_emitter: MakeEmitter, merge_rig: MergeRig
) -> None:
    """AC-BI-008, atomic: the group is logged whole or not at all; nothing partial is left."""
    emitter, _log_path = make_emitter()
    _seed_merge(merge_rig, nodes=_CAPABILITY_LOSER_NODES, edges=_CAPABILITY_LOSER_EDGES)
    merge_rig.store.fail_appends()

    with pytest.raises(CompanyMergePersistenceError, match=r"^FalkorDB write failed: "):
        _merge(merge_rig, emitter)

    assert ("Capability", _INCOMING) in merge_rig.policy_system.nodes
    assert ("PendingReview", _REVIEW_ID) in merge_rig.policy_system.nodes
    assert merge_rig.legacy.edge_count("REQUIRES") == 2
    assert merge_rig.legacy.edge_count("GOVERNED_BY") == 1


def test_the_loser_edge_reads_are_issued_once_each_after_the_existence_check(
    make_emitter: MakeEmitter, merge_rig: MergeRig
) -> None:
    """Review kind, ONE combined existence read, then the five loser-edge reads (ids via params)."""
    emitter, _log_path = make_emitter()
    _seed_merge(merge_rig)

    _merge(merge_rig, emitter)

    statements = merge_rig.legacy.statements
    assert len(statements) == 7
    assert "MATCH (r:PendingReview {id: $review_id}) RETURN" in statements[0]
    assert "incoming_created_at" in statements[1]
    assert "existing_created_at" in statements[1]
    assert all("$loser_id" in q for q in statements[2:])
    assert merge_rig.legacy.params_seen[2:] == [{"loser_id": _INCOMING}] * 5


def test_resolve_merge_of_nonexistent_review_returns_none_before_any_write(
    merge_rig: MergeRig,
) -> None:
    """AC-BI-008 (not-found half, merge): an unknown/already-resolved id makes no graph changes."""
    target = GraphWriteTarget(UnusedSubmitter(), POLICY_SYSTEM, merge_rig.audit_event_id)

    outcome = resolve_review(merge_rig.legacy, "review_missing", "merge", target)

    assert outcome is None
    assert len(merge_rig.legacy.statements) == 1


def test_resolve_merge_when_review_vanishes_before_existence_check_returns_none(
    merge_rig: MergeRig,
) -> None:
    """Benign race: Step 1 found the review, but it's gone by Step 2 -- treated as not-found."""
    _seed_merge(merge_rig)

    class _VanishingReview(RigReadHandle):
        def _read(self, q: str, params: dict[str, object] | None) -> list[object]:
            rows = super()._read(q, params)
            if q == "MATCH (r:PendingReview {id: $review_id}) RETURN r.kind":
                self._graph.nodes.pop(("PendingReview", _REVIEW_ID), None)
            return rows

    vanishing = _VanishingReview(merge_rig.policy_system)
    target = GraphWriteTarget(UnusedSubmitter(), POLICY_SYSTEM, merge_rig.audit_event_id)

    outcome = resolve_review(vanishing, _REVIEW_ID, "merge", target)

    assert outcome is None
    assert len(vanishing.statements) == 2


@pytest.mark.parametrize("missing", [_INCOMING, _EXISTING])
def test_stale_review_either_node_missing_raises_stale_error_and_writes_nothing(
    missing: str, make_emitter: MakeEmitter, read_lines: ReadLines, merge_rig: MergeRig
) -> None:
    """AC-BI-004 / CHANGES.md H2 / issue #190: nothing is submitted, logged or changed."""
    emitter, log_path = make_emitter()
    _seed_merge(merge_rig)
    del merge_rig.policy_system.nodes[("Capability", missing)]
    before = {key: dict(props) for key, props in merge_rig.policy_system.nodes.items()}
    target = GraphWriteTarget(UnusedSubmitter(), POLICY_SYSTEM, merge_rig.audit_event_id)

    with pytest.raises(StalePendingReviewError) as excinfo:
        resolve_review(merge_rig.legacy, _REVIEW_ID, "merge", target, emitter=emitter)
    emitter.flush()

    assert excinfo.value.review_id == _REVIEW_ID
    assert len(merge_rig.legacy.statements) == 2  # the review read and the existence read only
    assert merge_rig.policy_system.nodes == before
    assert merge_rig.logged() == []
    assert read_lines(log_path) == []


def test_the_resolve_log_entry_carries_winner_id_and_loser_id(
    make_emitter: MakeEmitter, read_lines: ReadLines, merge_rig: MergeRig
) -> None:
    """AC-BI-009 (full): merge logs decision, review id, timestamp, AND winner/loser ids."""
    emitter, log_path = make_emitter()
    _seed_merge(merge_rig)

    _merge(merge_rig, emitter)
    emitter.flush()

    entries = read_lines(log_path)
    assert len(entries) == 1
    entry = entries[0]
    assert entry["component"] == "company_merge"
    assert entry["action"] == "resolve_near_miss_review"
    assert entry["entity_id"] == "review_aaa"
    assert entry["outcome"] == "merge"
    assert entry["winner_id"] == _EXISTING
    assert entry["loser_id"] == _INCOMING
    assert "timestamp" in entry


# --- falkordb_live (issue #35, Slice 4, CHANGES.md H1: REQUIRED, not optional) ---

_LIVE_TEST_GRAPH = "policy_system_slice4_merge_live_test"


def _live_target(graph_name: str) -> GraphWriteTarget:
    """The real gateway on the disposable graph, under one system audit row (issue #210).

    Not runnable in the implementation sandbox (needs the PS state Postgres and FalkorDB).
    """
    config = load_config()
    audit_event_id = build_system_audit_recorder(config)(
        action="ingestion_run.submit",
        resource_type="ingestion_run",
        resource_id=f"live-{graph_name}",
        details={"trigger": "live_resolve_review"},
    )
    return GraphWriteTarget(build_default_graph_write_gateway(config), graph_name, audit_event_id)


@pytest.mark.falkordb_live
def test_resolve_merge_repoints_edges_and_deletes_loser_live(
    make_emitter: MakeEmitter, read_lines: ReadLines
) -> None:
    """AC-BI-005/006/007 against a REAL FalkorDB instance (CHANGES.md H1's required gate).

    This is the live anchor of the file: since #210 the merge is a read-resolve-write group, and
    only a real FalkorDB proves the read (`RETURN DISTINCT`) and the gateway application agree
    with the pre-#210 single statement on the row-multiplication scenario below. (Not run in the
    implementation sandbox.)

    Writes into a dedicated, disposable graph name
    (`policy_system_slice4_merge_live_test`), NEVER the real, shared
    `policy_system` graph -- mirrors `test_baseline_graph_isolation.py`'s
    and `test_live_capstone.py`'s own established "a database that already
    holds real regulatory data is not something this test mutates without a
    human's separately-obtained permission" convention (CLAUDE.md). The
    disposable graph is deleted in a `finally` block regardless of outcome,
    including a failing run, so no live residue survives (per this slice's
    own "leave the real FalkorDB instance clean" requirement) -- a
    defensive pre-clean also runs first, in case a prior failed run left
    residue.

    Seeds TWO `Obligation`s `REQUIRES`-ing the same loser `Capability`
    (mirroring `docs/artifacts/ps-domain-concepts.md:652-681`'s worked
    example -- the domain's *primary* convergence scenario, not an edge
    case) plus a `Policy` the loser is `GOVERNED_BY`, so the loser's edge read genuinely returns
    several rows that must collapse to one primitive per edge and one loser delete (the condition
    CHANGES.md C1 identified for the pre-#210 statement).
    """
    db = connect_from_config(load_config())

    existing_graphs = set(db.list_graphs())
    if _LIVE_TEST_GRAPH in existing_graphs:
        db.select_graph(_LIVE_TEST_GRAPH).delete()

    winner_id = "cap_winner_live_slice4"
    loser_id = "cap_loser_live_slice4"
    obligation_a_id = "obl_a_live_slice4"
    obligation_b_id = "obl_b_live_slice4"
    policy_id = "pol_live_slice4"
    review_id = "review_live_slice4_merge"

    emitter, log_path = make_emitter()
    try:
        graph = select_graph(db, _LIVE_TEST_GRAPH)

        # Winner: earlier created_at (2020). Loser: later created_at (2024). M2: earlier wins.
        graph.query(
            "CREATE (:Capability {id: $id, name: 'Incident Notification (winner)', "
            "created_at: '2020-01-01T00:00:00+00:00'})",
            params={"id": winner_id},
        )
        graph.query(
            "CREATE (:Capability {id: $id, name: 'Incident Notification (loser)', "
            "created_at: '2024-01-01T00:00:00+00:00'})",
            params={"id": loser_id},
        )
        # Two Obligations REQUIRES-ing the loser -- the multi-edge, row-multiplication scenario.
        graph.query(
            "MATCH (c:Capability {id: $loser_id}) "
            "CREATE (:Obligation {id: $obligation_id})-[:REQUIRES]->(c)",
            params={"loser_id": loser_id, "obligation_id": obligation_a_id},
        )
        graph.query(
            "MATCH (c:Capability {id: $loser_id}) "
            "CREATE (:Obligation {id: $obligation_id})-[:REQUIRES]->(c)",
            params={"loser_id": loser_id, "obligation_id": obligation_b_id},
        )
        # The loser GOVERNED_BY a Policy -- the second edge type C1's query re-points.
        graph.query(
            "MATCH (c:Capability {id: $loser_id}) "
            "CREATE (c)-[:GOVERNED_BY]->(:Policy {id: $policy_id, name: 'Live Policy'})",
            params={"loser_id": loser_id, "policy_id": policy_id},
        )
        graph.query(
            "CREATE (:PendingReview {id: $id, kind: 'Capability', status: 'pending', "
            "incoming_id: $incoming_id, incoming_text: 'incoming near-miss text', "
            "nearest_existing_id: $nearest_existing_id, "
            "nearest_existing_text: 'existing near-miss text', "
            "similarity: 0.91, created_at: '2024-06-01T00:00:00+00:00'})",
            params={"id": review_id, "incoming_id": loser_id, "nearest_existing_id": winner_id},
        )

        # The actual call under test: the real resolve_review, against real FalkorDB.
        outcome = resolve_review(
            graph, review_id, "merge", _live_target(_LIVE_TEST_GRAPH), emitter=emitter
        )
        emitter.flush()

        assert outcome is not None, "resolve_review returned None -- review/nodes not found"
        assert outcome.review_id == review_id
        assert outcome.decision == "merge"
        assert outcome.winner_id == winner_id
        assert outcome.loser_id == loser_id

        # Both Obligations now REQUIRES the winner (not the deleted loser).
        obligations_result = graph.query(
            "MATCH (o:Obligation)-[:REQUIRES]->(c:Capability {id: $winner_id}) "
            "RETURN o.id ORDER BY o.id",
            params={"winner_id": winner_id},
        )
        obligation_rows = cast("list[list[object]]", obligations_result.result_set)
        assert sorted(cast("str", row[0]) for row in obligation_rows) == sorted(
            [obligation_a_id, obligation_b_id]
        )

        # No duplicate REQUIRES edges were left behind (DISTINCT collapsed row multiplication).
        requires_count_result = graph.query(
            "MATCH (:Obligation)-[r:REQUIRES]->(:Capability {id: $winner_id}) RETURN count(r)",
            params={"winner_id": winner_id},
        )
        requires_count_rows = cast("list[list[object]]", requires_count_result.result_set)
        assert requires_count_rows[0][0] == 2

        # The winner is now GOVERNED_BY the same Policy the loser used to be.
        governed_by_result = graph.query(
            "MATCH (c:Capability {id: $winner_id})-[:GOVERNED_BY]->(p:Policy) RETURN p.id",
            params={"winner_id": winner_id},
        )
        governed_by_rows = cast("list[list[object]]", governed_by_result.result_set)
        assert [cast("str", row[0]) for row in governed_by_rows] == [policy_id]

        # The loser node no longer exists.
        loser_result = graph.query(
            "MATCH (c:Capability {id: $loser_id}) RETURN c", params={"loser_id": loser_id}
        )
        assert loser_result.result_set == []

        # The PendingReview node no longer exists.
        review_result = graph.query(
            "MATCH (r:PendingReview {id: $review_id}) RETURN r", params={"review_id": review_id}
        )
        assert review_result.result_set == []

        # AC-BI-009 (full): the structured log entry names winner/loser too.
        entries = read_lines(log_path)
        assert len(entries) == 1
        assert entries[0]["component"] == "company_merge"
        assert entries[0]["action"] == "resolve_near_miss_review"
        assert entries[0]["entity_id"] == review_id
        assert entries[0]["outcome"] == "merge"
        assert entries[0]["winner_id"] == winner_id
        assert entries[0]["loser_id"] == loser_id
    finally:
        db.select_graph(_LIVE_TEST_GRAPH).delete()
        remaining_graphs = set(db.list_graphs())
        assert _LIVE_TEST_GRAPH not in remaining_graphs
