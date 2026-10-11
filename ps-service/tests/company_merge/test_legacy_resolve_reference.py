"""The frozen near-miss resolution statements and their interpreter (issue #210, slice 10a).

`_legacy_resolve_reference` keeps the pre-#210 `CASE`-winner merge statement verbatim and applies it
clause by clause. This file proves:

* the frozen texts are the original ones (sha256 pins taken from `git show HEAD:`);
* each clause does what Cypher does: the winner rule (older `created_at` wins, a missing one wins,
  a tie and both-missing go to the incoming node), the five `FOREACH` rewires, `DETACH DELETE`,
  the silent no-ops of the leading and trailing `MATCH`;
* each clause of the winner predicate matters: the four mutations of CHANGES.md F5d (`<=` to `<`,
  `coalesce` dropped on either side or on both) change the outcome of a scenario, and change the
  pinned hash;
* `pending_review.resolve_review` leaves the same graph as the reference (A2). Slice 10a made the
  comparison against the then-current legacy write (same statements, same graph); slice 10b moved
  production onto the gateway and the comparison became the digest, modulo edge identity.

Met modulo edge identity; the live digest and live interpreter agreement are not verifiable here
(no FalkorDB in this sandbox).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

import pytest

from company_merge._gateway_fakes import POLICY_SYSTEM
from company_merge._legacy_reference import LegacyRecordingGraph, write_legacy_resolve_merge
from company_merge._legacy_resolve_reference import (
    FROZEN_RESOLVE_HASHES,
    RESOLVE_EXISTENCE_TEMPLATE,
    RESOLVE_FIND_REVIEW_QUERY,
    WINNER_PREDICATE,
    render_resolve_merge,
)
from ps_service.company_merge.graph_submission import GraphWriteTarget
from ps_service.company_merge.pending_review import resolve_review
from ps_service.graph_gateway.digest import canonical_digest

if TYPE_CHECKING:
    from company_merge._fakes import MakeEmitter
    from company_merge._gateway_fakes import MergeRig

_REVIEW = "review_aaa"
_INCOMING = "cap_incoming"
_EXISTING = "cap_existing"
_OLD = "2020-01-01T00:00:00+00:00"
_NEW = "2024-01-01T00:00:00+00:00"
_BARE: dict[str, object] = {}


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _graph(
    incoming_created: str | None,
    existing_created: str | None,
    *,
    kind: str = "Capability",
    extra_nodes: tuple[tuple[str, str, dict[str, object]], ...] = (),
    edges: tuple[tuple[str, str, str, str, str], ...] = (),
    with_review: bool = True,
) -> LegacyRecordingGraph:
    graph = LegacyRecordingGraph()
    for node_id, created in ((_INCOMING, incoming_created), (_EXISTING, existing_created)):
        props: dict[str, object] = {"name": node_id}
        if created is not None:
            props["created_at"] = created
        graph.nodes[(kind, node_id)] = props
    if with_review:
        graph.nodes[("PendingReview", _REVIEW)] = {
            "kind": kind,
            "incoming_id": _INCOMING,
            "nearest_existing_id": _EXISTING,
        }
    for label, node_id, props in extra_nodes:
        graph.nodes[(label, node_id)] = dict(props)
    for edge in edges:
        graph.edges[edge] = {}
    return graph


def _run(graph: LegacyRecordingGraph) -> tuple[str, str] | None:
    return write_legacy_resolve_merge(graph, _REVIEW)


# --- the pins ---------------------------------------------------------------------------------


def test_frozen_resolution_statements_match_their_pinned_hashes() -> None:
    assert _sha(RESOLVE_FIND_REVIEW_QUERY) == FROZEN_RESOLVE_HASHES["find_review"]
    for kind in ("Capability", "Policy"):
        assert (
            _sha(RESOLVE_EXISTENCE_TEMPLATE.format(kind=kind))
            == FROZEN_RESOLVE_HASHES[f"existence_{kind.lower()}"]
        )
        assert _sha(render_resolve_merge(kind)) == FROZEN_RESOLVE_HASHES[f"merge_{kind.lower()}"]


# --- the winner rule, one clause at a time --------------------------------------------------------


@pytest.mark.parametrize(
    ("incoming_created", "existing_created", "winner", "loser"),
    [
        pytest.param(_OLD, _NEW, _INCOMING, _EXISTING, id="incoming-older"),
        pytest.param(_NEW, _OLD, _EXISTING, _INCOMING, id="existing-older"),
        pytest.param(_OLD, _OLD, _INCOMING, _EXISTING, id="tie-goes-to-incoming"),
        pytest.param(None, _OLD, _INCOMING, _EXISTING, id="incoming-missing-wins"),
        pytest.param(_OLD, None, _EXISTING, _INCOMING, id="existing-missing-wins"),
        pytest.param(None, None, _INCOMING, _EXISTING, id="both-missing-goes-to-incoming"),
    ],
)
@pytest.mark.parametrize("kind", ["Capability", "Policy"])
def test_the_winner_rule(
    kind: str, incoming_created: str | None, existing_created: str | None, winner: str, loser: str
) -> None:
    graph = _graph(incoming_created, existing_created, kind=kind)

    assert _run(graph) == (winner, loser)
    assert (kind, loser) not in graph.nodes
    assert (kind, winner) in graph.nodes
    assert ("PendingReview", _REVIEW) not in graph.nodes


# --- the five rewired families, DETACH DELETE and the silent no-ops -------------------------------


_FAMILIES = [
    pytest.param(
        ("REQUIRES", "Obligation", "obl_a", "Capability", _EXISTING),
        ("REQUIRES", "Obligation", "obl_a", "Capability", _INCOMING),
        (("Obligation", "obl_a", _BARE),),
        id="inbound-REQUIRES",
    ),
    pytest.param(
        ("GOVERNED_BY", "Capability", _EXISTING, "Policy", "pol_a"),
        ("GOVERNED_BY", "Capability", _INCOMING, "Policy", "pol_a"),
        (("Policy", "pol_a", _BARE),),
        id="outbound-GOVERNED_BY",
    ),
    pytest.param(
        ("SUPPORTED_BY", "Policy", _EXISTING, "Standard", "std_a"),
        ("SUPPORTED_BY", "Policy", _INCOMING, "Standard", "std_a"),
        (("Standard", "std_a", _BARE),),
        id="outbound-SUPPORTED_BY",
    ),
    pytest.param(
        ("MERGED_INTO", "Capability", "cap_tomb", "Capability", _EXISTING),
        ("MERGED_INTO", "Capability", "cap_tomb", "Capability", _INCOMING),
        (("Capability", "cap_tomb", {"status": "merged"}),),
        id="inbound-MERGED_INTO",
    ),
]


@pytest.mark.parametrize(("rewired", "seeded", "extra"), _FAMILIES)
def test_each_family_of_the_loser_is_rewired_onto_the_winner(
    rewired: tuple[str, str, str, str, str],
    seeded: tuple[str, str, str, str, str],
    extra: tuple[tuple[str, str, dict[str, object]], ...],
) -> None:
    """The loser here is the incoming node (it is newer); the edge ends up on the existing one."""
    kind = "Policy" if seeded[0] == "SUPPORTED_BY" else "Capability"
    graph = _graph(_NEW, _OLD, kind=kind, extra_nodes=extra, edges=(seeded,))

    assert _run(graph) == (_EXISTING, _INCOMING)

    assert set(graph.edges) == {rewired}


def test_inbound_governed_by_of_a_policy_loser_is_rewired_onto_the_winner() -> None:
    """The fifth family: `(c:Capability)-[:GOVERNED_BY]->(loser)` for a Policy review."""
    graph = _graph(
        _NEW,
        _OLD,
        kind="Policy",
        extra_nodes=(("Capability", "cap_a", {}),),
        edges=(("GOVERNED_BY", "Capability", "cap_a", "Policy", _INCOMING),),
    )

    assert _run(graph) == (_EXISTING, _INCOMING)

    assert set(graph.edges) == {("GOVERNED_BY", "Capability", "cap_a", "Policy", _EXISTING)}


def test_several_obligations_requiring_the_loser_are_each_rewired_and_it_is_deleted_once() -> None:
    """The C1 row-multiplication scenario: `WITH DISTINCT` keeps one row after each block."""
    graph = _graph(
        _NEW,
        _OLD,
        extra_nodes=(("Obligation", "obl_a", _BARE), ("Obligation", "obl_b", {})),
        edges=(
            ("REQUIRES", "Obligation", "obl_a", "Capability", _INCOMING),
            ("REQUIRES", "Obligation", "obl_b", "Capability", _INCOMING),
        ),
    )

    assert _run(graph) == (_EXISTING, _INCOMING)

    assert set(graph.edges) == {
        ("REQUIRES", "Obligation", "obl_a", "Capability", _EXISTING),
        ("REQUIRES", "Obligation", "obl_b", "Capability", _EXISTING),
    }


def test_an_edge_the_winner_already_has_is_not_duplicated() -> None:
    graph = _graph(
        _NEW,
        _OLD,
        extra_nodes=(("Obligation", "obl_a", _BARE),),
        edges=(
            ("REQUIRES", "Obligation", "obl_a", "Capability", _INCOMING),
            ("REQUIRES", "Obligation", "obl_a", "Capability", _EXISTING),
        ),
    )

    _run(graph)

    assert set(graph.edges) == {("REQUIRES", "Obligation", "obl_a", "Capability", _EXISTING)}


def test_edges_outside_the_five_families_die_with_the_loser() -> None:
    graph = _graph(
        _NEW,
        _OLD,
        extra_nodes=(("PracticeArea", "pa_a", {}),),
        edges=(("COVERS", "PracticeArea", "pa_a", "Capability", _INCOMING),),
    )

    _run(graph)

    assert graph.edges == {}
    assert ("PracticeArea", "pa_a") in graph.nodes


def test_a_missing_node_makes_the_leading_match_find_nothing() -> None:
    graph = _graph(_NEW, _OLD)
    del graph.nodes[("Capability", _INCOMING)]
    before = dict(graph.nodes)

    assert _run(graph) is None
    assert graph.nodes == before


@pytest.mark.parametrize("missing", [_INCOMING, _EXISTING])
def test_a_missing_or_tombstoned_node_is_stale_and_nothing_is_written(missing: str) -> None:
    for tombstone in (False, True):
        graph = _graph(_NEW, _OLD)
        if tombstone:
            graph.nodes[("Capability", missing)]["status"] = "merged"
        else:
            del graph.nodes[("Capability", missing)]
        before = {key: dict(props) for key, props in graph.nodes.items()}

        assert _run(graph) is None
        assert graph.nodes == before


def test_a_review_gone_before_the_write_leaves_the_rewires_and_deletes_nothing() -> None:
    """The trailing `MATCH (rev ...)` finds no row: the earlier `FOREACH` blocks already ran."""
    graph = _graph(
        _NEW,
        _OLD,
        extra_nodes=(("Obligation", "obl_a", _BARE),),
        edges=(("REQUIRES", "Obligation", "obl_a", "Capability", _INCOMING),),
    )
    kinds = graph.query(RESOLVE_FIND_REVIEW_QUERY, {"review_id": _REVIEW})
    del graph.nodes[("PendingReview", _REVIEW)]
    assert kinds.result_set == [["Capability"]]

    merged = graph.query(
        render_resolve_merge("Capability"),
        {"incoming_id": _INCOMING, "nearest_existing_id": _EXISTING, "review_id": _REVIEW},
    )

    assert merged.result_set == []
    assert ("Capability", _INCOMING) in graph.nodes  # not deleted
    assert ("REQUIRES", "Obligation", "obl_a", "Capability", _EXISTING) in graph.edges  # rewired


def test_an_unknown_statement_shape_is_refused() -> None:
    graph = _graph(_NEW, _OLD)

    with pytest.raises(AssertionError, match="not a frozen legacy statement"):
        graph.query(render_resolve_merge("Capability") + " SET winner.x = 1", {"review_id": "x"})


# --- the mutations of the winner predicate (F5d) --------------------------------------------------


@dataclass(frozen=True)
class _Mutation:
    name: str
    predicate: str
    # (incoming, existing) created_at where the frozen predicate and this one disagree. Dropping the
    # existing side's coalesce only shows when the incoming side is '' too (both missing).
    scenario: tuple[str | None, str | None]


_MUTATIONS = [
    _Mutation(
        "lt-instead-of-lte", "coalesce(a.created_at, '') < coalesce(b.created_at, '')", (_OLD, _OLD)
    ),
    _Mutation("no-coalesce-incoming", "a.created_at <= coalesce(b.created_at, '')", (None, _OLD)),
    _Mutation("no-coalesce-existing", "coalesce(a.created_at, '') <= b.created_at", (None, None)),
    _Mutation("no-coalesce-either", "a.created_at <= b.created_at", (None, None)),
]


def _winner_with(statement: str, scenario: tuple[str | None, str | None]) -> str:
    graph = _graph(*scenario)
    result = graph.query(
        statement,
        {"incoming_id": _INCOMING, "nearest_existing_id": _EXISTING, "review_id": _REVIEW},
    )
    return str(cast("list[list[object]]", result.result_set)[0][0])


@pytest.mark.parametrize("mutation", _MUTATIONS, ids=lambda m: m.name)
def test_each_clause_of_the_winner_predicate_changes_the_outcome_and_the_pinned_hash(
    mutation: _Mutation,
) -> None:
    frozen = render_resolve_merge("Capability")
    assert frozen.count(WINNER_PREDICATE) == 2
    mutated = frozen.replace(WINNER_PREDICATE, mutation.predicate)

    assert _sha(mutated) != FROZEN_RESOLVE_HASHES["merge_capability"]
    assert _winner_with(frozen, mutation.scenario) == _INCOMING
    assert _winner_with(mutated, mutation.scenario) == _EXISTING


# --- A2: today's production issues the frozen statements and builds the same graph ---


@pytest.mark.parametrize(
    ("incoming_created", "existing_created"),
    [(_OLD, _NEW), (_NEW, _OLD), (_OLD, _OLD), (None, _OLD), (_OLD, None), (None, None)],
    ids=["incoming-older", "existing-older", "tie", "incoming-missing", "existing-missing", "both"],
)
def test_the_frozen_reference_and_current_production_build_the_same_graph_for_resolve_merge(
    incoming_created: str | None,
    existing_created: str | None,
    make_emitter: MakeEmitter,
    merge_rig: MergeRig,
) -> None:
    """Slice 10a compared today's legacy write with the reference; since 10b production writes a
    gateway group, so the comparison is the digest modulo edge identity (met modulo identity; live
    digest not verifiable here), over a graph with an edge of three families and one that dies.
    """
    emitter, _log_path = make_emitter()
    reference = _graph(
        incoming_created,
        existing_created,
        extra_nodes=(
            ("Obligation", "obl_a", _BARE),
            ("Policy", "pol_a", _BARE),
            ("Capability", "cap_tomb", {"status": "merged"}),
        ),
        edges=(
            ("REQUIRES", "Obligation", "obl_a", "Capability", _INCOMING),
            ("GOVERNED_BY", "Capability", _INCOMING, "Policy", "pol_a"),
            ("MERGED_INTO", "Capability", "cap_tomb", "Capability", _INCOMING),
            ("COVERS", "Policy", "pol_a", "Capability", _EXISTING),
        ),
    )
    reference.seed_into(merge_rig.policy_system)
    expected = write_legacy_resolve_merge(reference, _REVIEW)
    target = GraphWriteTarget(merge_rig.gateway, POLICY_SYSTEM, merge_rig.audit_event_id)

    outcome = resolve_review(merge_rig.legacy, _REVIEW, "merge", target, emitter=emitter)

    assert outcome is not None
    assert expected == (outcome.winner_id, outcome.loser_id)
    assert canonical_digest(merge_rig.policy_system) == canonical_digest(
        reference.as_gateway_graph()
    )
