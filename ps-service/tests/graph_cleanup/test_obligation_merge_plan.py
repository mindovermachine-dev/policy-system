"""Pure planner/validator for `merge-obligations` (issue #190, slice 11 sub-step a).

AC-BI-005 (preview content), AC-BI-007 (union plan, survivor keeps its single HAS, absorbed
deleted, full snapshot), AC-BI-015 (cross-role rejected), AC-BI-016 (self / nonexistent).
"""

from __future__ import annotations

import pytest

from ps_service.graph_cleanup.errors import GraphCleanupValidationError
from ps_service.graph_cleanup.models import (
    EdgeRecord,
    ObligationMergeState,
    ObligationNodeState,
    RequirementSourceRef,
    RoleRef,
)
from ps_service.graph_cleanup.obligation_planner import plan_obligation_merge

_S = "obl_survivor"
_A = "obl_absorbed"
_ROLE = RoleRef(id="role_1", name="Manufacturer")


def _node(node_id: str, text: str) -> ObligationNodeState:
    return ObligationNodeState(id=node_id, text=text, properties={"confidence": 0.9})


def _satisfied(requirement: str, target: str) -> EdgeRecord:
    return EdgeRecord(
        rel_type="SATISFIED_BY",
        source_label="Requirement",
        source_id=requirement,
        target_label="Obligation",
        target_id=target,
    )


def _requires(source: str, capability: str) -> EdgeRecord:
    return EdgeRecord(
        rel_type="REQUIRES",
        source_label="Obligation",
        source_id=source,
        target_label="Capability",
        target_id=capability,
    )


def _state(
    *,
    survivor: ObligationNodeState | None = None,
    absorbed: ObligationNodeState | None = None,
    survivor_roles: tuple[RoleRef, ...] = (_ROLE,),
    absorbed_roles: tuple[RoleRef, ...] = (_ROLE,),
    edges: tuple[EdgeRecord, ...] = (),
    refs: tuple[RequirementSourceRef, ...] = (),
) -> ObligationMergeState:
    return ObligationMergeState(
        survivor=survivor if survivor is not None else _node(_S, "Report incidents"),
        absorbed=absorbed if absorbed is not None else _node(_A, "Report  incidents."),
        survivor_roles=survivor_roles,
        absorbed_roles=absorbed_roles,
        edges=edges,
        absorbed_requirement_refs=refs,
    )


def _full_state() -> ObligationMergeState:
    return _state(
        edges=(
            _satisfied("req_1", _A),
            _satisfied("req_2", _A),
            _satisfied("req_2", _S),
            _satisfied("req_3", _S),
            _requires(_A, "cap_1"),
            _requires(_A, "cap_2"),
            _requires(_S, "cap_2"),
        ),
        refs=(
            RequirementSourceRef(requirement_id="req_1", source_ref="Art. 6(1)"),
            RequirementSourceRef(requirement_id="req_2", source_ref="Art. 6(2)"),
        ),
    )


def test_preview_lists_edges_to_union_and_collapsed_duplicates() -> None:
    plan = plan_obligation_merge(_full_state())

    preview = plan.preview
    assert preview.survivor_id == _S
    assert preview.absorbed_id == _A
    assert (preview.role_id, preview.role_name) == ("role_1", "Manufacturer")
    assert preview.edges_to_move.satisfied_by == ("req_1", "req_2")
    assert preview.edges_to_move.requires == ("cap_1", "cap_2")
    assert preview.duplicate_edges_collapsed == 2  # req_2 and cap_2 already on the survivor
    assert [r.source_ref for r in preview.requirement_source_refs] == ["Art. 6(1)", "Art. 6(2)"]
    assert preview.state_digest == plan.state_digest


def test_expected_counts_pin_the_absorbed_edges() -> None:
    plan = plan_obligation_merge(_full_state())

    assert plan.expected.satisfied == 2
    assert plan.expected.requires == 2


def test_before_snapshot_holds_the_absorbed_node_in_full_and_all_its_edges() -> None:
    plan = plan_obligation_merge(_full_state())

    absorbed = next(n for n in plan.before.nodes if n.id == _A)
    assert absorbed.label == "Obligation"
    assert absorbed.properties == {"text": "Report  incidents.", "confidence": 0.9}
    edges = {(e.rel_type, e.source_id, e.target_id) for e in plan.before.edges}
    assert {
        ("HAS", "role_1", _A),
        ("SATISFIED_BY", "req_1", _A),
        ("SATISFIED_BY", "req_2", _A),
        ("REQUIRES", _A, "cap_1"),
        ("REQUIRES", _A, "cap_2"),
    } <= edges


def test_after_snapshot_has_the_union_on_the_survivor_and_the_marker() -> None:
    plan = plan_obligation_merge(_full_state())

    assert [n.id for n in plan.after.nodes if n.label == "Obligation"] == [_S]
    marker = next(n for n in plan.after.nodes if n.label == "MergedObligation")
    assert marker.id == _A
    assert marker.properties == {"merged_into": _S}
    edges = {(e.rel_type, e.source_id, e.target_id) for e in plan.after.edges}
    assert edges == {
        ("HAS", "role_1", _S),
        ("SATISFIED_BY", "req_1", _S),
        ("SATISFIED_BY", "req_2", _S),
        ("SATISFIED_BY", "req_3", _S),
        ("REQUIRES", _S, "cap_1"),
        ("REQUIRES", _S, "cap_2"),
    }
    assert all(_A not in (e.source_id, e.target_id) for e in plan.after.edges)


def test_the_digest_is_independent_of_edge_order_and_changes_with_the_edge_set() -> None:
    state = _full_state()
    reordered = state.model_copy(update={"edges": tuple(reversed(state.edges))})
    grown = state.model_copy(update={"edges": (*state.edges, _requires(_A, "cap_9"))})

    assert (
        plan_obligation_merge(state).state_digest == plan_obligation_merge(reordered).state_digest
    )
    assert plan_obligation_merge(state).state_digest != plan_obligation_merge(grown).state_digest


def test_an_absorbed_obligation_with_no_edges_plans_an_empty_union() -> None:
    plan = plan_obligation_merge(_state())

    assert plan.preview.edges_to_move.satisfied_by == ()
    assert plan.expected.requires == 0


def test_obligations_under_different_roles_are_rejected() -> None:
    state = _state(absorbed_roles=(RoleRef(id="role_2", name="Importer"),))

    with pytest.raises(GraphCleanupValidationError) as excinfo:
        plan_obligation_merge(state)

    assert "different roles" in str(excinfo.value)
    assert "Manufacturer" in str(excinfo.value)
    assert "Importer" in str(excinfo.value)


@pytest.mark.parametrize(
    "roles", [(), (RoleRef(id="role_1", name="M"), RoleRef(id="role_2", name="N"))]
)
def test_a_side_not_borne_by_exactly_one_role_is_rejected(roles: tuple[RoleRef, ...]) -> None:
    with pytest.raises(GraphCleanupValidationError) as excinfo:
        plan_obligation_merge(_state(survivor_roles=roles))

    assert "exactly one role" in str(excinfo.value)


def test_a_self_merge_is_rejected() -> None:
    state = _state(absorbed=_node(_S, "same"))

    with pytest.raises(GraphCleanupValidationError) as excinfo:
        plan_obligation_merge(state)

    assert "itself" in str(excinfo.value)


@pytest.mark.parametrize("missing", ["survivor", "absorbed"])
def test_a_nonexistent_side_is_rejected(missing: str) -> None:
    base = _state()
    state = base.model_copy(update={missing: None})

    with pytest.raises(GraphCleanupValidationError) as excinfo:
        plan_obligation_merge(state)

    assert missing in str(excinfo.value)
    assert "does not exist" in str(excinfo.value)
