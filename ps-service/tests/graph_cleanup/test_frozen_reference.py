"""The frozen legacy reference is what the direct writer was.

The equivalence tests compare the gateway-backed cleanup against these statements, so the
statements must be the original ones and the interpreter must model only what it was taught.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, cast

import pytest

from graph_cleanup._legacy_reference import (
    FROZEN_STATEMENT_HASHES,
    FROZEN_STATEMENTS,
    LegacyRecordingGraph,
    legacy_merge_capabilities,
    legacy_merge_obligations,
    legacy_release_governance,
    legacy_unmerge_capability,
    legacy_unmerge_obligation,
)

if TYPE_CHECKING:
    from collections.abc import Callable

from ps_service.graph_cleanup.models import (
    CapabilityUnmergeWrite,
    ExpectedCounts,
    ObligationExpectedCounts,
    ObligationUnmergeWrite,
)


def test_frozen_statements_match_their_pinned_hashes() -> None:
    assert set(FROZEN_STATEMENTS) == set(FROZEN_STATEMENT_HASHES)
    for name, text in FROZEN_STATEMENTS.items():
        assert hashlib.sha256(text.encode()).hexdigest() == FROZEN_STATEMENT_HASHES[name], name


def test_the_interpreter_rejects_a_statement_shape_it_was_not_taught() -> None:
    graph = LegacyRecordingGraph()

    with pytest.raises(AssertionError, match="not a frozen legacy statement"):
        graph.query("MATCH (n) DETACH DELETE n", {"x": 1})


def _release_graph(
    *, capability_status: str | None = "active", policy_status: str = "draft", edge: bool = True
) -> LegacyRecordingGraph:
    graph = LegacyRecordingGraph()
    capability: dict[str, object] = {"name": "c"}
    if capability_status is not None:
        capability["status"] = capability_status
    graph.nodes[("Capability", "cap")] = capability
    graph.nodes[("Policy", "pol")] = {"status": policy_status}
    if edge:
        graph.edges[("GOVERNED_BY", "Capability", "cap", "Policy", "pol")] = {}
        graph.edges[("GOVERNED_BY", "Capability", "other", "Policy", "pol")] = {}
    return graph


def test_release_deletes_only_the_one_edge_when_the_guard_holds() -> None:
    graph = _release_graph()

    matched = legacy_release_governance(graph, capability_id="cap", policy_id="pol")

    assert matched is True
    assert list(graph.edges) == [("GOVERNED_BY", "Capability", "other", "Policy", "pol")]


def test_a_legacy_capability_without_a_status_counts_as_active() -> None:
    graph = _release_graph(capability_status=None)

    assert legacy_release_governance(graph, capability_id="cap", policy_id="pol") is True


@pytest.mark.parametrize(
    "graph",
    [
        _release_graph(edge=False),
        _release_graph(capability_status="merged"),
        _release_graph(capability_status="deprecated"),
        _release_graph(policy_status="approved"),
    ],
    ids=["edge-gone", "tombstone", "deprecated", "policy-left-draft"],
)
def test_release_returns_no_row_and_changes_nothing_when_any_guard_is_false(
    graph: LegacyRecordingGraph,
) -> None:
    before = dict(graph.edges)

    matched = legacy_release_governance(graph, capability_id="cap", policy_id="pol")

    assert matched is False
    assert graph.edges == before


# -- merge-capabilities ---------------------------------------------------------------------------

_COUNTS = ExpectedCounts(requires=2, covers=1, mitigated=0)


def _merge_graph(
    *, absorbed_policy: str | None = None, survivor_policy: str | None = None
) -> LegacyRecordingGraph:
    graph = LegacyRecordingGraph()
    graph.nodes[("Capability", "s")] = {"name": "S", "status": "active"}
    graph.nodes[("Capability", "a")] = {"name": "A"}  # no status: a legacy node counts as active
    for policy in {absorbed_policy, survivor_policy} - {None}:
        graph.nodes[("Policy", cast("str", policy))] = {"status": "approved"}
    for source in ("o1", "o2"):
        graph.edges[("REQUIRES", "Obligation", source, "Capability", "a")] = {}
    graph.edges[("REQUIRES", "Obligation", "o2", "Capability", "s")] = {}
    graph.edges[("COVERS", "PracticeArea", "pa", "Capability", "a")] = {}
    if absorbed_policy:
        graph.edges[("GOVERNED_BY", "Capability", "a", "Policy", absorbed_policy)] = {}
    if survivor_policy:
        graph.edges[("GOVERNED_BY", "Capability", "s", "Policy", survivor_policy)] = {}
    return graph


def _merge(graph: LegacyRecordingGraph, expected: ExpectedCounts = _COUNTS) -> bool:
    return legacy_merge_capabilities(graph, survivor_id="s", absorbed_id="a", expected=expected)


def test_a_case_one_merge_moves_each_edge_once_and_tombstones_the_absorbed() -> None:
    graph = _merge_graph()

    assert _merge(graph) is True

    assert set(graph.edges) == {
        ("REQUIRES", "Obligation", "o1", "Capability", "s"),
        ("REQUIRES", "Obligation", "o2", "Capability", "s"),
        ("COVERS", "PracticeArea", "pa", "Capability", "s"),
        ("MERGED_INTO", "Capability", "a", "Capability", "s"),
    }
    assert graph.nodes[("Capability", "a")]["status"] == "merged"
    assert graph.nodes[("Capability", "s")]["status"] == "active"


def test_a_governed_absorbed_capability_hands_its_policy_edge_to_an_ungoverned_survivor() -> None:
    graph = _merge_graph(absorbed_policy="p")
    expected = _COUNTS.model_copy(
        update={
            "absorbed_governed": 1,
            "absorbed_policy_id": "p",
            "absorbed_policy_status": "approved",
        }
    )

    assert _merge(graph, expected) is True

    governed = {k[2:5:2] for k in graph.edges if k[0] == "GOVERNED_BY"}
    assert governed == {("s", "p")}


def test_a_governed_survivor_keeps_its_policy_edge_and_the_absorbed_ones_are_dropped() -> None:
    graph = _merge_graph(absorbed_policy="p", survivor_policy="p")
    expected = _COUNTS.model_copy(
        update={
            "absorbed_governed": 1,
            "survivor_governed": 1,
            "absorbed_policy_id": "p",
            "absorbed_policy_status": "approved",
            "survivor_policy_id": "p",
            "survivor_policy_status": "approved",
        }
    )

    assert _merge(graph, expected) is True

    governed = {k[2:5:2] for k in graph.edges if k[0] == "GOVERNED_BY"}
    assert governed == {("s", "p")}


def test_two_different_policies_are_a_miss_even_when_every_count_matches() -> None:
    graph = _merge_graph(absorbed_policy="p", survivor_policy="q")
    expected = _COUNTS.model_copy(
        update={
            "absorbed_governed": 1,
            "survivor_governed": 1,
            "absorbed_policy_id": "p",
            "absorbed_policy_status": "approved",
            "survivor_policy_id": "q",
            "survivor_policy_status": "approved",
        }
    )
    before = dict(graph.edges)

    assert _merge(graph, expected) is False
    assert graph.edges == before


@pytest.mark.parametrize(
    "expected",
    [
        _COUNTS.model_copy(update={"requires": 1}),
        _COUNTS.model_copy(update={"covers": 0}),
        _COUNTS.model_copy(update={"mitigated": 1}),
        _COUNTS.model_copy(update={"absorbed_governed": 1}),
        _COUNTS.model_copy(update={"survivor_governed": 1}),
    ],
    ids=["requires", "covers", "mitigated", "absorbed-governed", "survivor-governed"],
)
def test_a_capability_merge_whose_counts_differ_returns_no_row_and_changes_nothing(
    expected: ExpectedCounts,
) -> None:
    graph = _merge_graph()
    before = (dict(graph.edges), {k: dict(v) for k, v in graph.nodes.items()})

    assert _merge(graph, expected) is False
    assert (graph.edges, graph.nodes) == before


@pytest.mark.parametrize("status", ["merged", "deprecated"])
def test_a_capability_merge_needs_both_sides_active(status: str) -> None:
    graph = _merge_graph()
    graph.nodes[("Capability", "s")]["status"] = status

    assert _merge(graph) is False


def test_a_policy_that_changed_status_is_a_miss() -> None:
    graph = _merge_graph(absorbed_policy="p")
    graph.nodes[("Policy", "p")]["status"] = "deprecated"
    expected = _COUNTS.model_copy(
        update={
            "absorbed_governed": 1,
            "absorbed_policy_id": "p",
            "absorbed_policy_status": "approved",
        }
    )

    assert _merge(graph, expected) is False


# -- merge-obligations ----------------------------------------------------------------------------

_OBLIGATION_COUNTS = ObligationExpectedCounts(satisfied=2, requires=1)


def _obligation_graph() -> LegacyRecordingGraph:
    graph = LegacyRecordingGraph()
    for obligation in ("s", "a", "x"):
        graph.nodes[("Obligation", obligation)] = {"text": obligation}
    graph.nodes[("Role", "r1")] = {}
    graph.nodes[("Role", "r2")] = {}
    for key in (
        ("HAS", "Role", "r1", "Obligation", "s"),
        ("HAS", "Role", "r1", "Obligation", "a"),
        ("HAS", "Role", "r2", "Obligation", "x"),
        ("SATISFIED_BY", "Requirement", "q1", "Obligation", "a"),
        ("SATISFIED_BY", "Requirement", "q2", "Obligation", "a"),
        ("SATISFIED_BY", "Requirement", "q2", "Obligation", "s"),
        ("REQUIRES", "Obligation", "a", "Capability", "c1"),
        ("REQUIRES", "Obligation", "s", "Capability", "c1"),
    ):
        graph.edges[key] = {}
    return graph


def _merge_obligations(
    graph: LegacyRecordingGraph,
    *,
    absorbed: str = "a",
    expected: ObligationExpectedCounts = _OBLIGATION_COUNTS,
) -> bool:
    return legacy_merge_obligations(graph, survivor_id="s", absorbed_id=absorbed, expected=expected)


def test_an_obligation_merge_unions_the_edges_marks_the_absorbed_and_detach_deletes_it() -> None:
    graph = _obligation_graph()

    assert _merge_obligations(graph) is True

    assert ("Obligation", "a") not in graph.nodes
    assert graph.nodes[("MergedObligation", "a")] == {"merged_into": "s"}
    assert set(graph.edges) == {
        ("HAS", "Role", "r1", "Obligation", "s"),
        ("HAS", "Role", "r2", "Obligation", "x"),
        ("SATISFIED_BY", "Requirement", "q1", "Obligation", "s"),
        ("SATISFIED_BY", "Requirement", "q2", "Obligation", "s"),
        ("REQUIRES", "Obligation", "s", "Capability", "c1"),
    }


def test_a_cross_role_pair_is_a_miss() -> None:
    graph = _obligation_graph()
    before = (dict(graph.edges), set(graph.nodes))

    assert (
        _merge_obligations(
            graph, absorbed="x", expected=ObligationExpectedCounts(satisfied=0, requires=0)
        )
        is False
    )
    assert (graph.edges, set(graph.nodes)) == before


def test_a_self_merge_is_a_miss() -> None:
    graph = _obligation_graph()

    assert _merge_obligations(graph, absorbed="s") is False


@pytest.mark.parametrize(
    "expected",
    [
        ObligationExpectedCounts(satisfied=1, requires=1),
        ObligationExpectedCounts(satisfied=2, requires=0),
    ],
    ids=["satisfied", "requires"],
)
def test_an_obligation_merge_whose_counts_differ_returns_no_row_and_changes_nothing(
    expected: ObligationExpectedCounts,
) -> None:
    graph = _obligation_graph()
    before = (dict(graph.edges), set(graph.nodes))

    assert _merge_obligations(graph, expected=expected) is False
    assert (graph.edges, set(graph.nodes)) == before
    assert ("MergedObligation", "a") not in graph.nodes


# -- unmerge-capability ---------------------------------------------------------------------------


def _unmerge_write(**changes: object) -> CapabilityUnmergeWrite:
    fields: dict[str, object] = {
        "restore_requires_ids": ("o1", "o2"),
        "remove_requires_ids": ("o1",),
        "restore_covers_ids": ("pa",),
        "remove_covers_ids": ("pa",),
        "restore_mitigated_ids": (),
        "remove_mitigated_ids": (),
        "restore_policy_id": None,
        "remove_policy_edge": False,
        **changes,
    }
    return CapabilityUnmergeWrite.model_validate(fields)


def _unmerge_graph(*, policy: str | None = None) -> LegacyRecordingGraph:
    """The aftermath of `_merge_graph()`'s merge, plus a survivor-added edge `o_new`."""
    graph = LegacyRecordingGraph()
    graph.nodes[("Capability", "s")] = {"name": "S", "status": "active"}
    graph.nodes[("Capability", "a")] = {"name": "A", "status": "merged"}
    graph.nodes[("Policy", "p")] = {"status": "approved"}
    for obligation in ("o1", "o2", "o_new"):
        graph.nodes[("Obligation", obligation)] = {}
        graph.edges[("REQUIRES", "Obligation", obligation, "Capability", "s")] = {}
    graph.nodes[("PracticeArea", "pa")] = {}
    graph.edges[("COVERS", "PracticeArea", "pa", "Capability", "s")] = {}
    graph.edges[("MERGED_INTO", "Capability", "a", "Capability", "s")] = {}
    if policy:
        graph.edges[("GOVERNED_BY", "Capability", "s", "Policy", policy)] = {}
    return graph


def _unmerge(graph: LegacyRecordingGraph, write: CapabilityUnmergeWrite | None = None) -> bool:
    return legacy_unmerge_capability(
        graph, survivor_id="s", absorbed_id="a", write=write or _unmerge_write()
    )


def test_a_capability_unmerge_restores_the_moved_edges_and_keeps_the_survivor_added_one() -> None:
    graph = _unmerge_graph()

    assert _unmerge(graph) is True

    assert set(graph.edges) == {
        ("REQUIRES", "Obligation", "o1", "Capability", "a"),
        ("REQUIRES", "Obligation", "o2", "Capability", "a"),
        ("REQUIRES", "Obligation", "o2", "Capability", "s"),
        ("REQUIRES", "Obligation", "o_new", "Capability", "s"),
        ("COVERS", "PracticeArea", "pa", "Capability", "a"),
    }
    assert graph.nodes[("Capability", "a")]["status"] == "active"


def test_a_capability_unmerge_moves_the_policy_edge_back_only_when_it_was_moved() -> None:
    moved = _unmerge_graph(policy="p")
    kept = _unmerge_graph(policy="p")

    assert _unmerge(moved, _unmerge_write(restore_policy_id="p", remove_policy_edge=True))
    assert _unmerge(kept, _unmerge_write(restore_policy_id="p", remove_policy_edge=False))

    assert ("GOVERNED_BY", "Capability", "a", "Policy", "p") in moved.edges
    assert ("GOVERNED_BY", "Capability", "s", "Policy", "p") not in moved.edges
    assert ("GOVERNED_BY", "Capability", "a", "Policy", "p") in kept.edges
    assert ("GOVERNED_BY", "Capability", "s", "Policy", "p") in kept.edges


def _not_a_tombstone(g: LegacyRecordingGraph) -> None:
    g.nodes[("Capability", "a")] = {"status": "active"}


def _no_redirect(g: LegacyRecordingGraph) -> None:
    del g.edges[("MERGED_INTO", "Capability", "a", "Capability", "s")]


def _survivor_merged(g: LegacyRecordingGraph) -> None:
    g.nodes[("Capability", "s")] = {"status": "merged"}


def _endpoint_gone(g: LegacyRecordingGraph) -> None:
    del g.nodes[("Obligation", "o1")]


def _edge_gone(g: LegacyRecordingGraph) -> None:
    del g.edges[("REQUIRES", "Obligation", "o1", "Capability", "s")]


def _area_gone(g: LegacyRecordingGraph) -> None:
    del g.nodes[("PracticeArea", "pa")]


@pytest.mark.parametrize(
    "break_it",
    [_not_a_tombstone, _no_redirect, _survivor_merged, _endpoint_gone, _edge_gone, _area_gone],
)
def test_a_capability_unmerge_whose_guard_misses_returns_no_row_and_changes_nothing(
    break_it: Callable[[LegacyRecordingGraph], object],
) -> None:
    graph = _unmerge_graph()
    break_it(graph)
    before = (dict(graph.nodes), dict(graph.edges))

    assert _unmerge(graph) is False
    assert (dict(graph.nodes), dict(graph.edges)) == before


def test_a_capability_unmerge_needs_the_policy_and_the_survivors_governed_by_edge() -> None:
    no_policy = _unmerge_graph(policy="p")
    del no_policy.nodes[("Policy", "p")]
    no_edge = _unmerge_graph()

    assert _unmerge(no_policy, _unmerge_write(restore_policy_id="p")) is False
    assert (
        _unmerge(no_edge, _unmerge_write(restore_policy_id="p", remove_policy_edge=True)) is False
    )


# -- unmerge-obligation ---------------------------------------------------------------------------


def _obl_write() -> ObligationUnmergeWrite:
    return ObligationUnmergeWrite(
        role_id="r",
        satisfied_by_ids=("q1", "q2"),
        requires_ids=("c",),
        properties={"text": "Report", "confidence": 0.8},
    )


def _obl_graph() -> LegacyRecordingGraph:
    graph = LegacyRecordingGraph()
    graph.nodes[("MergedObligation", "a")] = {"merged_into": "s"}
    graph.nodes[("Obligation", "s")] = {"text": "Report"}
    graph.nodes[("Role", "r")] = {}
    graph.nodes[("Requirement", "q1")] = {}
    graph.nodes[("Requirement", "q2")] = {}
    graph.nodes[("Capability", "c")] = {"status": "active"}
    graph.edges[("HAS", "Role", "r", "Obligation", "s")] = {}
    graph.edges[("SATISFIED_BY", "Requirement", "q2", "Obligation", "s")] = {}
    return graph


def _obl_unmerge(graph: LegacyRecordingGraph) -> bool:
    return legacy_unmerge_obligation(graph, survivor_id="s", absorbed_id="a", write=_obl_write())


def test_an_obligation_unmerge_recreates_the_node_and_its_edges_and_drops_the_marker() -> None:
    graph = _obl_graph()

    assert _obl_unmerge(graph) is True

    assert graph.nodes[("Obligation", "a")] == {"text": "Report", "confidence": 0.8}
    assert ("MergedObligation", "a") not in graph.nodes
    assert set(graph.edges) == {
        ("HAS", "Role", "r", "Obligation", "s"),
        ("SATISFIED_BY", "Requirement", "q2", "Obligation", "s"),
        ("HAS", "Role", "r", "Obligation", "a"),
        ("SATISFIED_BY", "Requirement", "q1", "Obligation", "a"),
        ("SATISFIED_BY", "Requirement", "q2", "Obligation", "a"),
        ("REQUIRES", "Obligation", "a", "Capability", "c"),
    }


def _no_marker(g: LegacyRecordingGraph) -> None:
    del g.nodes[("MergedObligation", "a")]


def _marker_repointed(g: LegacyRecordingGraph) -> None:
    g.nodes[("MergedObligation", "a")] = {"merged_into": "x"}


def _exists_again(g: LegacyRecordingGraph) -> None:
    g.nodes[("Obligation", "a")] = {}


def _role_gone(g: LegacyRecordingGraph) -> None:
    del g.nodes[("Role", "r")]


def _requirement_gone(g: LegacyRecordingGraph) -> None:
    del g.nodes[("Requirement", "q1")]


def _capability_gone(g: LegacyRecordingGraph) -> None:
    del g.nodes[("Capability", "c")]


def _capability_tombstone(g: LegacyRecordingGraph) -> None:
    g.nodes[("Capability", "c")] = {"status": "merged"}


@pytest.mark.parametrize(
    "break_it",
    [
        _no_marker,
        _marker_repointed,
        _exists_again,
        _role_gone,
        _requirement_gone,
        _capability_gone,
        _capability_tombstone,
    ],
)
def test_an_obligation_unmerge_whose_guard_misses_returns_no_row_and_changes_nothing(
    break_it: Callable[[LegacyRecordingGraph], object],
) -> None:
    graph = _obl_graph()
    break_it(graph)
    before = (dict(graph.nodes), dict(graph.edges))

    assert _obl_unmerge(graph) is False
    assert (dict(graph.nodes), dict(graph.edges)) == before
