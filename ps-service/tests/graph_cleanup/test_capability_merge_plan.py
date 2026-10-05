"""Pure planner/validator for `merge-capabilities`, case 1 (issue #190, slice 10 sub-step a).

AC-BI-005 (preview content), AC-BI-016 (self / nonexistent / tombstone rejected),
AC-BI-021 (snapshot shape sufficient to reverse), case restriction until slices 12/13.
"""

from __future__ import annotations

import pytest

from ps_service.graph_cleanup.errors import GraphCleanupValidationError
from ps_service.graph_cleanup.merge_planner import plan_capability_merge
from ps_service.graph_cleanup.models import (
    CapabilityNodeState,
    EdgeRecord,
    GoverningPolicy,
    MergeState,
)

_S = "cap_survivor"
_A = "cap_absorbed"


def _node(node_id: str, *, status: str = "active") -> CapabilityNodeState:
    return CapabilityNodeState(
        id=node_id, name=f"name of {node_id}", status=status, properties={"description": "d"}
    )


def _edge(rel: str, label: str, source: str, target: str) -> EdgeRecord:
    return EdgeRecord(
        rel_type=rel,
        source_label=label,
        source_id=source,
        target_label="Capability",
        target_id=target,
    )


def _state(
    *,
    survivor: CapabilityNodeState | None = None,
    absorbed: CapabilityNodeState | None = None,
    edges: tuple[EdgeRecord, ...] = (),
    survivor_policies: tuple[GoverningPolicy, ...] = (),
    absorbed_policies: tuple[GoverningPolicy, ...] = (),
) -> MergeState:
    return MergeState(
        survivor=survivor if survivor is not None else _node(_S),
        absorbed=absorbed if absorbed is not None else _node(_A),
        edges=edges,
        survivor_policies=survivor_policies,
        absorbed_policies=absorbed_policies,
    )


def _full_state() -> MergeState:
    return _state(
        edges=(
            _edge("REQUIRES", "Obligation", "obl_1", _A),
            _edge("REQUIRES", "Obligation", "obl_2", _A),
            _edge("REQUIRES", "Obligation", "obl_2", _S),
            _edge("COVERS", "PracticeArea", "pa_1", _A),
            _edge("MITIGATED_BY", "RiskPath", "rp_1", _A),
            _edge("REQUIRES", "Obligation", "obl_3", _S),
        )
    )


def test_preview_lists_edges_to_move_and_collapsed_duplicates() -> None:
    plan = plan_capability_merge(_full_state())

    preview = plan.preview
    assert preview.survivor_id == _S
    assert preview.absorbed_id == _A
    assert preview.policy_case == 1
    assert preview.edges_to_move.requires == ("obl_1", "obl_2")
    assert preview.edges_to_move.covers == ("pa_1",)
    assert preview.edges_to_move.mitigated_by == ("rp_1",)
    assert preview.duplicate_edges_collapsed == 1
    assert preview.obligations_affected == 2


def test_expected_counts_are_the_absorbed_edge_counts() -> None:
    plan = plan_capability_merge(_full_state())

    assert plan.expected.requires == 2
    assert plan.expected.covers == 1
    assert plan.expected.mitigated == 1
    assert plan.expected.absorbed_governed == 0
    assert plan.expected.survivor_governed == 0


def test_before_snapshot_holds_both_nodes_and_every_incident_edge() -> None:
    plan = plan_capability_merge(_full_state())

    before = plan.before
    assert {(n.label, n.id) for n in before.nodes} == {("Capability", _S), ("Capability", _A)}
    absorbed = next(n for n in before.nodes if n.id == _A)
    assert absorbed.properties["status"] == "active"
    assert absorbed.properties["name"] == f"name of {_A}"
    assert len(before.edges) == 6


def test_after_snapshot_moves_edges_without_duplicates_and_adds_the_tombstone() -> None:
    plan = plan_capability_merge(_full_state())

    after = plan.after
    absorbed = next(n for n in after.nodes if n.id == _A)
    assert absorbed.properties["status"] == "merged"
    edges = {(e.rel_type, e.source_id, e.target_id) for e in after.edges}
    assert len(edges) == len(after.edges)
    assert ("MERGED_INTO", _A, _S) in edges
    assert ("REQUIRES", "obl_1", _S) in edges
    assert ("REQUIRES", "obl_2", _S) in edges
    assert ("REQUIRES", "obl_3", _S) in edges
    assert ("COVERS", "pa_1", _S) in edges
    assert ("MITIGATED_BY", "rp_1", _S) in edges
    assert not any(target == _A and rel != "MERGED_INTO" for rel, _s, target in edges)


def test_state_digest_is_stable_and_changes_when_an_edge_changes() -> None:
    first = plan_capability_merge(_full_state()).state_digest
    second = plan_capability_merge(_full_state()).state_digest
    changed = plan_capability_merge(
        _state(edges=(_edge("REQUIRES", "Obligation", "obl_9", _A),))
    ).state_digest

    assert first == second
    assert first != changed
    assert len(first) == 64


def test_digest_ignores_edge_order() -> None:
    state = _full_state()
    reordered = state.model_copy(update={"edges": tuple(reversed(state.edges))})

    assert (
        plan_capability_merge(state).state_digest == plan_capability_merge(reordered).state_digest
    )


def test_self_merge_is_rejected() -> None:
    state = _state(absorbed=_node(_S))

    with pytest.raises(GraphCleanupValidationError, match="itself"):
        plan_capability_merge(state)


def test_missing_either_side_is_rejected() -> None:
    for missing in ("survivor", "absorbed"):
        state = MergeState(
            survivor=None if missing == "survivor" else _node(_S),
            absorbed=None if missing == "absorbed" else _node(_A),
            edges=(),
            survivor_policies=(),
            absorbed_policies=(),
        )
        with pytest.raises(GraphCleanupValidationError, match="does not exist"):
            plan_capability_merge(state)


@pytest.mark.parametrize("side", ["survivor", "absorbed"])
def test_a_merged_tombstone_on_either_side_is_rejected(side: str) -> None:
    tombstone = _node(_S if side == "survivor" else _A, status="merged")
    state = _state(survivor=tombstone) if side == "survivor" else _state(absorbed=tombstone)

    with pytest.raises(GraphCleanupValidationError, match="merged"):
        plan_capability_merge(state)


def test_a_deprecated_capability_is_rejected_as_not_active() -> None:
    with pytest.raises(GraphCleanupValidationError, match="not active"):
        plan_capability_merge(_state(absorbed=_node(_A, status="deprecated")))


_POLICY = GoverningPolicy(id="pol_1", title="P", status="draft")


def test_both_governed_by_the_same_policy_is_planned_as_case_three() -> None:
    state = _state(survivor_policies=(_POLICY,), absorbed_policies=(_POLICY,))

    assert plan_capability_merge(state).preview.policy_case == 3
