"""Pure planner for capability `unmerge` (issue #190, slice 15 sub-step a; AC-BI-019/020/021).

The merge audit details are produced by the REAL merge planner, so the unmerge planner is
proven against the snapshot shape the executor actually records.
"""

from __future__ import annotations

import pytest

from ps_service.graph_cleanup.audit_actions import CapabilityMergeDetails
from ps_service.graph_cleanup.errors import GraphCleanupValidationError
from ps_service.graph_cleanup.merge_planner import plan_capability_merge
from ps_service.graph_cleanup.models import (
    CapabilityNodeState,
    CapabilityUnmergeState,
    EdgeRecord,
    GoverningPolicy,
    MergeState,
)
from ps_service.graph_cleanup.unmerge_planner import (
    derive_capability_unmerge_inputs,
    plan_capability_unmerge,
)

_S = "cap_survivor"
_A = "cap_absorbed"
_POLICY = GoverningPolicy(id="pol_1", title="Incident Policy", status="approved")


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


_MERGE_EDGES = (
    _edge("REQUIRES", "Obligation", "obl_1", _A),
    _edge("REQUIRES", "Obligation", "obl_2", _A),
    _edge("REQUIRES", "Obligation", "obl_2", _S),
    _edge("COVERS", "PracticeArea", "pa_1", _A),
    _edge("MITIGATED_BY", "RiskPath", "rp_1", _A),
)


def _details(
    *,
    survivor_policies: tuple[GoverningPolicy, ...] = (),
    absorbed_policies: tuple[GoverningPolicy, ...] = (),
) -> CapabilityMergeDetails:
    state = MergeState(
        survivor=_node(_S),
        absorbed=_node(_A),
        edges=_MERGE_EDGES,
        survivor_policies=survivor_policies,
        absorbed_policies=absorbed_policies,
        governed_sets={p.id: (_A, _S) for p in (*survivor_policies, *absorbed_policies)},
    )
    plan = plan_capability_merge(state)
    return CapabilityMergeDetails(
        survivor_id=_S,
        absorbed_id=_A,
        policy_case=plan.preview.policy_case,
        acknowledged=True,
        approval_id="merge-approval-1",
        before=plan.before,
        after=plan.after,
    )


def _live(
    *,
    survivor_status: str = "active",
    absorbed_status: str = "merged",
    redirects: dict[str, tuple[str, ...]] | None = None,
    survivor_edges: tuple[EdgeRecord, ...] | None = None,
    survivor_policy_ids: tuple[str, ...] = (),
    missing_endpoints: tuple[str, ...] = (),
    policy_exists: bool = True,
) -> CapabilityUnmergeState:
    """The graph right after the merge unless overridden."""
    after_survivor = survivor_edges
    if after_survivor is None:
        after_survivor = (
            _edge("REQUIRES", "Obligation", "obl_1", _S),
            _edge("REQUIRES", "Obligation", "obl_2", _S),
            _edge("COVERS", "PracticeArea", "pa_1", _S),
            _edge("MITIGATED_BY", "RiskPath", "rp_1", _S),
        )
    endpoints = {
        "REQUIRES": tuple(x for x in ("obl_1", "obl_2") if x not in missing_endpoints),
        "COVERS": tuple(x for x in ("pa_1",) if x not in missing_endpoints),
        "MITIGATED_BY": tuple(x for x in ("rp_1",) if x not in missing_endpoints),
    }
    return CapabilityUnmergeState(
        absorbed=_node(_A, status=absorbed_status),
        survivor=_node(_S, status=survivor_status),
        redirects=redirects if redirects is not None else {_A: (_S,)},
        survivor_edges=after_survivor,
        survivor_policy_ids=survivor_policy_ids,
        existing_endpoints=endpoints,
        policy_exists=policy_exists,
    )


def test_inputs_restore_every_absorbed_edge_and_remove_only_what_the_merge_moved() -> None:
    inputs = derive_capability_unmerge_inputs(_details())

    assert inputs.survivor_id == _S
    assert inputs.absorbed_id == _A
    assert inputs.merge_approval_id == "merge-approval-1"
    assert inputs.restore_requires == ("obl_1", "obl_2")
    assert inputs.restore_covers == ("pa_1",)
    assert inputs.restore_mitigated == ("rp_1",)
    # obl_2 already required the survivor before the merge: it stays on the survivor.
    assert inputs.remove_requires == ("obl_1",)
    assert inputs.remove_covers == ("pa_1",)
    assert inputs.remove_mitigated == ("rp_1",)
    assert inputs.restore_policy_id is None
    assert inputs.remove_policy_edge is False


def test_a_clean_unmerge_previews_the_restore_and_the_removal() -> None:
    plan = plan_capability_unmerge(derive_capability_unmerge_inputs(_details()), _live())

    preview = plan.preview
    assert preview.kind == "capability"
    assert (preview.merged_id, preview.survivor_id) == (_A, _S)
    assert preview.merge_approval_id == "merge-approval-1"
    assert preview.edges_to_restore.requires == ("obl_1", "obl_2")
    assert preview.edges_to_restore.covers == ("pa_1",)
    assert preview.edges_to_restore.mitigated_by == ("rp_1",)
    assert preview.edges_to_restore.governed_by is None
    assert preview.edges_removed_from_survivor.requires == ("obl_1",)
    assert preview.survivor_added_edges == ()
    assert preview.state_digest == plan.state_digest


def test_edges_added_to_the_survivor_since_the_merge_stay_and_are_listed() -> None:
    added = _edge("REQUIRES", "Obligation", "obl_new", _S)
    live = _live(
        survivor_edges=(
            _edge("REQUIRES", "Obligation", "obl_1", _S),
            _edge("REQUIRES", "Obligation", "obl_2", _S),
            _edge("COVERS", "PracticeArea", "pa_1", _S),
            _edge("MITIGATED_BY", "RiskPath", "rp_1", _S),
            added,
        )
    )

    plan = plan_capability_unmerge(derive_capability_unmerge_inputs(_details()), live)

    assert plan.preview.survivor_added_edges == (added,)
    assert plan.write.remove_requires_ids == ("obl_1",)
    assert "obl_new" not in plan.write.remove_requires_ids


def test_an_edge_the_merge_moved_that_has_left_the_survivor_is_not_removed_again() -> None:
    live = _live(
        survivor_edges=(
            _edge("REQUIRES", "Obligation", "obl_2", _S),
            _edge("COVERS", "PracticeArea", "pa_1", _S),
            _edge("MITIGATED_BY", "RiskPath", "rp_1", _S),
        )
    )

    plan = plan_capability_unmerge(derive_capability_unmerge_inputs(_details()), live)

    assert plan.write.restore_requires_ids == ("obl_1", "obl_2")
    assert plan.write.remove_requires_ids == ()


def test_the_snapshots_reverse_the_merge() -> None:
    details = _details()
    plan = plan_capability_unmerge(derive_capability_unmerge_inputs(details), _live())

    assert details.before is not None
    absorbed_before = {(e.rel_type, e.source_id) for e in details.before.edges if e.target_id == _A}
    absorbed_after = {(e.rel_type, e.source_id) for e in plan.after.edges if e.target_id == _A}
    assert absorbed_after == absorbed_before
    assert all(e.rel_type != "MERGED_INTO" for e in plan.after.edges)
    assert any(e.rel_type == "MERGED_INTO" for e in plan.before.edges)
    statuses = {n.id: n.properties["status"] for n in plan.after.nodes if n.label == "Capability"}
    assert statuses[_A] == "active"
    assert {n.properties["status"] for n in plan.before.nodes if n.id == _A} == {"merged"}


def test_the_digest_binds_the_current_state() -> None:
    inputs = derive_capability_unmerge_inputs(_details())
    base = plan_capability_unmerge(inputs, _live()).state_digest
    again = plan_capability_unmerge(inputs, _live()).state_digest
    changed = plan_capability_unmerge(
        inputs,
        _live(
            survivor_edges=(
                _edge("REQUIRES", "Obligation", "obl_1", _S),
                _edge("REQUIRES", "Obligation", "obl_2", _S),
                _edge("REQUIRES", "Obligation", "obl_new", _S),
                _edge("COVERS", "PracticeArea", "pa_1", _S),
                _edge("MITIGATED_BY", "RiskPath", "rp_1", _S),
            )
        ),
    ).state_digest

    assert base == again
    assert base != changed


def test_a_merge_record_without_a_snapshot_cannot_be_reversed() -> None:
    details = _details().model_copy(update={"before": None, "after": None})

    with pytest.raises(GraphCleanupValidationError, match="snapshot"):
        derive_capability_unmerge_inputs(details)


# --- governance -------------------------------------------------------------------------------


def test_case_two_with_a_governed_absorbed_capability_moves_the_policy_edge_back() -> None:
    inputs = derive_capability_unmerge_inputs(_details(absorbed_policies=(_POLICY,)))
    assert inputs.restore_policy_id == "pol_1"
    assert inputs.remove_policy_edge is True

    plan = plan_capability_unmerge(inputs, _live(survivor_policy_ids=("pol_1",)))

    assert plan.preview.edges_to_restore.governed_by == "pol_1"
    assert plan.write.restore_policy_id == "pol_1"
    assert plan.write.remove_policy_edge is True
    assert ("GOVERNED_BY", _A, "pol_1") in {
        (e.rel_type, e.source_id, e.target_id) for e in plan.after.edges
    }
    assert ("GOVERNED_BY", _S, "pol_1") not in {
        (e.rel_type, e.source_id, e.target_id) for e in plan.after.edges
    }


@pytest.mark.parametrize("now_governed_by", [(), ("pol_other",), ("pol_1", "pol_other")])
def test_case_two_conflicts_when_the_survivors_governance_changed(
    now_governed_by: tuple[str, ...],
) -> None:
    inputs = derive_capability_unmerge_inputs(_details(absorbed_policies=(_POLICY,)))

    with pytest.raises(GraphCleanupValidationError, match="governance"):
        plan_capability_unmerge(inputs, _live(survivor_policy_ids=now_governed_by))


def test_case_two_with_a_governed_survivor_leaves_the_absorbed_ungoverned() -> None:
    inputs = derive_capability_unmerge_inputs(_details(survivor_policies=(_POLICY,)))
    assert inputs.restore_policy_id is None

    plan = plan_capability_unmerge(inputs, _live(survivor_policy_ids=("pol_other",)))

    assert plan.preview.edges_to_restore.governed_by is None
    assert plan.write.remove_policy_edge is False


def test_case_three_restores_the_deleted_edge_and_keeps_the_survivors() -> None:
    inputs = derive_capability_unmerge_inputs(
        _details(survivor_policies=(_POLICY,), absorbed_policies=(_POLICY,))
    )
    assert inputs.restore_policy_id == "pol_1"
    assert inputs.remove_policy_edge is False

    plan = plan_capability_unmerge(inputs, _live(survivor_policy_ids=("pol_1",)))

    assert plan.write.restore_policy_id == "pol_1"
    assert plan.write.remove_policy_edge is False


def test_case_three_conflicts_when_the_survivor_is_no_longer_governed_by_that_policy() -> None:
    inputs = derive_capability_unmerge_inputs(
        _details(survivor_policies=(_POLICY,), absorbed_policies=(_POLICY,))
    )

    with pytest.raises(GraphCleanupValidationError, match="governance"):
        plan_capability_unmerge(inputs, _live(survivor_policy_ids=("pol_2",)))


def test_a_restore_policy_that_no_longer_exists_is_a_conflict() -> None:
    inputs = derive_capability_unmerge_inputs(_details(absorbed_policies=(_POLICY,)))

    with pytest.raises(GraphCleanupValidationError, match="pol_1"):
        plan_capability_unmerge(inputs, _live(survivor_policy_ids=("pol_1",), policy_exists=False))


# --- conflicts (AC-BI-020), each explained -------------------------------------------------------


def test_a_survivor_that_was_merged_away_is_a_conflict_naming_where_it_went() -> None:
    inputs = derive_capability_unmerge_inputs(_details())
    live = _live(survivor_status="merged", redirects={_A: (_S,), _S: ("cap_winner",)})

    with pytest.raises(GraphCleanupValidationError, match="cap_winner") as caught:
        plan_capability_unmerge(inputs, live)

    assert _S in str(caught.value)


def test_a_tombstone_redirected_elsewhere_by_a_near_miss_merge_names_the_current_target() -> None:
    inputs = derive_capability_unmerge_inputs(_details())
    live = _live(redirects={_A: ("cap_winner",)})

    with pytest.raises(GraphCleanupValidationError, match="cap_winner"):
        plan_capability_unmerge(inputs, live)


def test_a_survivor_that_no_longer_exists_is_a_conflict() -> None:
    inputs = derive_capability_unmerge_inputs(_details())
    live = _live().model_copy(update={"survivor": None})

    with pytest.raises(GraphCleanupValidationError, match="no longer"):
        plan_capability_unmerge(inputs, live)


def test_a_missing_snapshot_endpoint_is_a_conflict_listing_it() -> None:
    inputs = derive_capability_unmerge_inputs(_details())

    with pytest.raises(GraphCleanupValidationError, match="obl_1") as caught:
        plan_capability_unmerge(inputs, _live(missing_endpoints=("obl_1",)))

    assert "no longer exists" in str(caught.value)


def test_an_absorbed_capability_that_is_already_active_has_nothing_to_unmerge() -> None:
    inputs = derive_capability_unmerge_inputs(_details())

    with pytest.raises(GraphCleanupValidationError, match="not a merged tombstone"):
        plan_capability_unmerge(inputs, _live(absorbed_status="active", redirects={}))


def test_a_missing_absorbed_capability_is_a_conflict() -> None:
    inputs = derive_capability_unmerge_inputs(_details())
    live = _live().model_copy(update={"absorbed": None})

    with pytest.raises(GraphCleanupValidationError, match="no longer exists"):
        plan_capability_unmerge(inputs, live)


def test_a_tombstone_without_a_redirect_is_a_conflict() -> None:
    inputs = derive_capability_unmerge_inputs(_details())

    with pytest.raises(GraphCleanupValidationError, match="MERGED_INTO"):
        plan_capability_unmerge(inputs, _live(redirects={}))
