"""Pure planner for obligation `unmerge` (issue #190, slice 16 sub-step a; AC-BI-019/020/021).

The merge audit details are produced by the REAL obligation-merge planner, so the unmerge planner
is proven against the snapshot shape the executor actually records.
"""

from __future__ import annotations

import pytest

from ps_service.graph_cleanup.audit_actions import ObligationMergeDetails
from ps_service.graph_cleanup.errors import GraphCleanupValidationError
from ps_service.graph_cleanup.models import (
    EdgeRecord,
    ObligationMergeState,
    ObligationNodeState,
    ObligationUnmergeState,
    RoleRef,
)
from ps_service.graph_cleanup.obligation_planner import plan_obligation_merge
from ps_service.graph_cleanup.unmerge_planner import (
    derive_obligation_unmerge_inputs,
    plan_obligation_unmerge,
)

_S = "obl_survivor"
_A = "obl_absorbed"
_ROLE = RoleRef(id="role_1", name="Manufacturer")


def _sat(requirement: str, obligation: str) -> EdgeRecord:
    return EdgeRecord(
        rel_type="SATISFIED_BY",
        source_label="Requirement",
        source_id=requirement,
        target_label="Obligation",
        target_id=obligation,
    )


def _req(obligation: str, capability: str) -> EdgeRecord:
    return EdgeRecord(
        rel_type="REQUIRES",
        source_label="Obligation",
        source_id=obligation,
        target_label="Capability",
        target_id=capability,
    )


def _details() -> ObligationMergeDetails:
    state = ObligationMergeState(
        survivor=ObligationNodeState(
            id=_S, text="Report incidents", properties={"confidence": 0.9}
        ),
        absorbed=ObligationNodeState(
            id=_A, text="Report  incidents.", properties={"confidence": 0.8}
        ),
        survivor_roles=(_ROLE,),
        absorbed_roles=(_ROLE,),
        edges=(
            _sat("req_1", _A),
            _sat("req_2", _A),
            _sat("req_2", _S),
            _req(_A, "cap_1"),
            _req(_S, "cap_1"),
        ),
    )
    plan = plan_obligation_merge(state)
    return ObligationMergeDetails(
        survivor_id=_S,
        absorbed_id=_A,
        role_id=_ROLE.id,
        approval_id="merge-approval-1",
        before=plan.before,
        after=plan.after,
    )


def _live(**overrides: object) -> ObligationUnmergeState:
    """The graph right after the merge unless overridden."""
    base: dict[str, object] = {
        "absorbed_exists": False,
        "survivor_text": "Report incidents",
        "survivor_exists": True,
        "marker_targets": {_A: (_S,)},
        "role_exists": True,
        "existing_requirements": ("req_1", "req_2"),
        "capability_statuses": {"cap_1": "active"},
        "survivor_edges": (_sat("req_1", _S), _sat("req_2", _S), _req(_S, "cap_1")),
    }
    return ObligationUnmergeState.model_validate({**base, **overrides})


def test_inputs_carry_the_absorbed_node_in_full_and_every_edge_it_had() -> None:
    inputs = derive_obligation_unmerge_inputs(_details())

    assert (inputs.survivor_id, inputs.absorbed_id, inputs.role_id) == (_S, _A, "role_1")
    assert inputs.merge_approval_id == "merge-approval-1"
    assert inputs.properties == {"confidence": 0.8, "text": "Report  incidents."}
    assert inputs.satisfied_by_ids == ("req_1", "req_2")
    assert inputs.requires_ids == ("cap_1",)


def test_a_clean_unmerge_previews_the_recreation_under_the_original_id() -> None:
    plan = plan_obligation_unmerge(derive_obligation_unmerge_inputs(_details()), _live())

    preview = plan.preview
    assert preview.kind == "obligation"
    assert (preview.merged_id, preview.survivor_id, preview.role_id) == (_A, _S, "role_1")
    assert preview.merged_text == "Report  incidents."
    assert preview.merge_approval_id == "merge-approval-1"
    assert preview.edges_to_restore.satisfied_by == ("req_1", "req_2")
    assert preview.edges_to_restore.requires == ("cap_1",)
    assert preview.state_digest == plan.state_digest
    assert plan.write.properties == {"confidence": 0.8, "text": "Report  incidents."}
    assert plan.write.role_id == "role_1"


def test_survivor_edges_that_may_come_from_the_merge_are_listed_and_not_removed() -> None:
    plan = plan_obligation_unmerge(derive_obligation_unmerge_inputs(_details()), _live())

    # req_1 was only on the absorbed node before the merge; req_2 / cap_1 the survivor already had.
    assert plan.preview.survivor_edges_possibly_from_merge == (_sat("req_1", _S),)
    assert plan.preview.survivor_added_edges == ()
    assert "may originate from the merge" in plan.preview.note
    assert not hasattr(plan.write, "remove_satisfied_by_ids")


def test_edges_added_to_the_survivor_since_the_merge_stay_and_are_listed() -> None:
    added = _req(_S, "cap_new")
    live = _live(
        survivor_edges=(_sat("req_1", _S), _sat("req_2", _S), _req(_S, "cap_1"), added),
        capability_statuses={"cap_1": "active", "cap_new": "active"},
    )

    plan = plan_obligation_unmerge(derive_obligation_unmerge_inputs(_details()), live)

    assert plan.preview.survivor_added_edges == (added,)
    assert plan.write.requires_ids == ("cap_1",)


def test_the_snapshots_reverse_the_merge() -> None:
    plan = plan_obligation_unmerge(derive_obligation_unmerge_inputs(_details()), _live())

    after_nodes = {(n.label, n.id) for n in plan.after.nodes}
    assert ("Obligation", _A) in after_nodes
    assert {(n.label, n.id) for n in plan.before.nodes} >= {("MergedObligation", _A)}
    restored = {(e.rel_type, e.source_id, e.target_id) for e in plan.after.edges}
    assert ("HAS", "role_1", _A) in restored
    assert ("SATISFIED_BY", "req_1", _A) in restored
    assert ("REQUIRES", _A, "cap_1") in restored


def test_the_digest_binds_the_current_state() -> None:
    inputs = derive_obligation_unmerge_inputs(_details())
    base = plan_obligation_unmerge(inputs, _live()).state_digest
    changed = plan_obligation_unmerge(
        inputs, _live(survivor_edges=(_sat("req_1", _S), _req(_S, "cap_1")))
    ).state_digest

    assert base == plan_obligation_unmerge(inputs, _live()).state_digest
    assert base != changed


def test_a_merge_record_without_a_snapshot_cannot_be_reversed() -> None:
    details = _details().model_copy(update={"before": None, "after": None})

    with pytest.raises(GraphCleanupValidationError, match="snapshot"):
        derive_obligation_unmerge_inputs(details)


# --- conflicts (AC-BI-020), each explained -------------------------------------------------------


def test_an_obligation_that_exists_again_has_nothing_to_unmerge() -> None:
    inputs = derive_obligation_unmerge_inputs(_details())

    with pytest.raises(GraphCleanupValidationError, match="already exists"):
        plan_obligation_unmerge(inputs, _live(absorbed_exists=True))


def test_a_missing_marker_is_a_conflict() -> None:
    inputs = derive_obligation_unmerge_inputs(_details())

    with pytest.raises(GraphCleanupValidationError, match="MergedObligation"):
        plan_obligation_unmerge(inputs, _live(marker_targets={}))


def test_a_marker_pointing_elsewhere_names_the_current_target() -> None:
    inputs = derive_obligation_unmerge_inputs(_details())

    with pytest.raises(GraphCleanupValidationError, match="obl_winner"):
        plan_obligation_unmerge(inputs, _live(marker_targets={_A: ("obl_winner",)}))


def test_a_survivor_that_was_merged_away_is_a_conflict_naming_where_it_went() -> None:
    inputs = derive_obligation_unmerge_inputs(_details())
    live = _live(survivor_exists=False, marker_targets={_A: (_S,), _S: ("obl_winner",)})

    with pytest.raises(GraphCleanupValidationError, match="obl_winner"):
        plan_obligation_unmerge(inputs, live)


def test_a_survivor_that_no_longer_exists_is_a_conflict() -> None:
    inputs = derive_obligation_unmerge_inputs(_details())

    with pytest.raises(GraphCleanupValidationError, match="survivor obligation"):
        plan_obligation_unmerge(inputs, _live(survivor_exists=False))


def test_a_missing_role_is_a_conflict() -> None:
    inputs = derive_obligation_unmerge_inputs(_details())

    with pytest.raises(GraphCleanupValidationError, match="role_1"):
        plan_obligation_unmerge(inputs, _live(role_exists=False))


def test_a_missing_requirement_is_a_conflict_listing_it() -> None:
    inputs = derive_obligation_unmerge_inputs(_details())

    with pytest.raises(GraphCleanupValidationError, match="req_1"):
        plan_obligation_unmerge(inputs, _live(existing_requirements=("req_2",)))


@pytest.mark.parametrize("statuses", [{}, {"cap_1": "merged"}])
def test_a_capability_endpoint_that_is_gone_or_a_tombstone_is_a_conflict(
    statuses: dict[str, str],
) -> None:
    inputs = derive_obligation_unmerge_inputs(_details())

    with pytest.raises(GraphCleanupValidationError, match="cap_1"):
        plan_obligation_unmerge(inputs, _live(capability_statuses=statuses))
