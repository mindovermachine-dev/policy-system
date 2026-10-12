"""`unmerge` of a Capability writes through the Graph Write Gateway (issue #212, slice S5).

One approved unmerge is one gateway group: the edges the merge moved are upserted back onto the
absorbed Capability, ONLY the planned edges leave the survivor (never an edge it gained since),
the policy edge moves back when the merge moved it, and the tombstone becomes `active` again with
its `MERGED_INTO` edge deleted. The group is linked to the `applied` audit row and guarded by the
log position read BEFORE the guard reads (AC-BI-003/005/006/008). The guard is the old statement's
`WHERE`, evaluated in Python.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
import redis.exceptions

from graph_cleanup._fakes import ABSORBED, SURVIVOR
from graph_cleanup._gateway_fakes import POLICY, POLICY_SYSTEM, CleanupRig
from graph_cleanup._merge_runs import roles
from graph_cleanup._unmerge_runs import (
    capability_unmerge_world,
    run_capability_unmerge,
    unmerge_plan,
    unmerge_row,
)
from ps_service.api.errors import GraphWriteUnavailableError
from ps_service.graph_cleanup.dependencies import GraphCleanupDependencies
from ps_service.graph_cleanup.graph_reader import read_capability_unmerge_state
from ps_service.graph_cleanup.group_builders import (
    build_capability_unmerge_group,
    capability_unmerge_guard_holds,
)
from ps_service.graph_cleanup.models import CapabilityUnmergePlan
from ps_service.graph_gateway.errors import GraphUnavailableError, GraphWriteRejectedError
from ps_service.graph_gateway.models import (
    DeleteEdge,
    ExpectedPosition,
    MergeProperty,
    NodeRef,
    UpsertEdge,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from ps_service.config import ServiceConfig

_NODE_READ = "RETURN c.id, c.name"
"""A distinctive part of the first read of the unmerge state: its first run (when the approval row
is built) is the replan, the second the guard."""
_WRITE_FAILED = (
    "the unmerge could not be completed; check its status with check-cleanup-approval before "
    "retrying"
)

_ABSORBED = NodeRef(label="Capability", id=ABSORBED)
_SURVIVOR = NodeRef(label="Capability", id=SURVIVOR)
_POLICY = NodeRef(label="Policy", id=POLICY)


def _restore(edge_type: str, label: str, node_id: str) -> UpsertEdge:
    return UpsertEdge(
        type=edge_type,
        identity=f"{edge_type}:{node_id}->{ABSORBED}",
        source=NodeRef(label=label, id=node_id),
        target=_ABSORBED,
    )


def _remove(edge_type: str, label: str, node_id: str) -> DeleteEdge:
    return DeleteEdge(
        type=edge_type,
        identity=f"{edge_type}:{node_id}->{SURVIVOR}",
        source=NodeRef(label=label, id=node_id),
        target=_SURVIVOR,
    )


_RESTORES = (
    _restore("REQUIRES", "Obligation", "obl_1"),
    _restore("REQUIRES", "Obligation", "obl_2"),
    _restore("COVERS", "PracticeArea", "pa_1"),
    _restore("MITIGATED_BY", "RiskPath", "rp_1"),
)
_REMOVES = (
    _remove("REQUIRES", "Obligation", "obl_1"),
    _remove("COVERS", "PracticeArea", "pa_1"),
    _remove("MITIGATED_BY", "RiskPath", "rp_1"),
)
_RESTORE_POLICY = UpsertEdge(
    type="GOVERNED_BY",
    identity=f"GOVERNED_BY:{ABSORBED}->{POLICY}",
    source=_ABSORBED,
    target=_POLICY,
)
_REMOVE_POLICY = DeleteEdge(
    type="GOVERNED_BY",
    identity=f"GOVERNED_BY:{SURVIVOR}->{POLICY}",
    source=_SURVIVOR,
    target=_POLICY,
)
_TAIL = (
    MergeProperty(label="Capability", id=ABSORBED, properties={"status": "active"}),
    DeleteEdge(
        type="MERGED_INTO",
        identity=f"MERGED_INTO:{ABSORBED}->{SURVIVOR}",
        source=_ABSORBED,
        target=_SURVIVOR,
    ),
)
_CASES = {
    None: (*_RESTORES, *_REMOVES, *_TAIL),
    "moved": (*_RESTORES, *_REMOVES, _RESTORE_POLICY, _REMOVE_POLICY, *_TAIL),
    "kept": (*_RESTORES, *_REMOVES, _RESTORE_POLICY, *_TAIL),
}


def _rows(rig: CleanupRig) -> list[tuple[str, str]]:
    return [(r.action, r.outcome) for r in rig.audit.rows]


def _into(rig: CleanupRig, node_id: str) -> list[tuple[str, str]]:
    return sorted(
        (k[0], k[2]) for k in rig.graph.edges if k[4] == node_id and k[0] != "MERGED_INTO"
    )


# -- the group (AC-BI-005, AC-BI-006) -------------------------------------------------------------


@pytest.mark.parametrize("policy", [None, "moved", "kept"], ids=["case1", "case2", "case3"])
def test_one_unmerge_is_exactly_one_group_with_the_ordered_primitives(
    cleanup_rig: CleanupRig, policy: str | None
) -> None:
    capability_unmerge_world(cleanup_rig, policy=policy)
    position = cleanup_rig.store.last_position(POLICY_SYSTEM)

    outcome = run_capability_unmerge(cleanup_rig)

    assert outcome == {
        "merged_id": ABSORBED,
        "survivor_id": SURVIVOR,
        "unmerged": True,
        "survivor_added_edges": [],
    }
    [group] = cleanup_rig.hooked.groups
    assert group.graph == POLICY_SYSTEM
    assert group.primitives == _CASES[policy]
    assert group.preconditions == (ExpectedPosition(position=position),)


def test_the_pure_build_names_the_same_primitives(cleanup_rig: CleanupRig) -> None:
    capability_unmerge_world(cleanup_rig, policy="moved")
    plan = unmerge_plan(cleanup_rig, ABSORBED)
    assert isinstance(plan, CapabilityUnmergePlan)

    assert (
        build_capability_unmerge_group(plan.write, absorbed_id=ABSORBED, survivor_id=SURVIVOR)
        == _CASES["moved"]
    )


def test_the_groups_audit_event_id_is_the_applied_rows_id(cleanup_rig: CleanupRig) -> None:
    capability_unmerge_world(cleanup_rig)

    run_capability_unmerge(cleanup_rig)

    [event_id] = cleanup_rig.audit.event_ids
    [group] = cleanup_rig.hooked.groups
    assert group.audit_event_id == event_id
    assert _rows(cleanup_rig) == [("capability.unmerge", "applied")]
    assert cleanup_rig.order() == ["audit:applied", "log_append"]


def test_the_position_is_read_before_the_guard_reads_and_the_guard_before_the_submit(
    cleanup_rig: CleanupRig,
) -> None:
    capability_unmerge_world(cleanup_rig)
    row = unmerge_row(cleanup_rig, ABSORBED)
    calls = cleanup_rig.hooked.calls
    cleanup_rig.read.flip_before(_NODE_READ, 2, lambda: calls.append("guard_read"))

    run_capability_unmerge(cleanup_rig, row=row)

    assert calls == [
        "is_caught_up",
        "read_log_position",
        "guard_read",
        "is_caught_up",  # the stamp needs a caught-up graph, whatever the order
        "stamp_legacy_edge_identity",
        "submit_group",
    ]


def test_the_graph_after_the_group_has_the_edges_back_and_the_tombstone_gone(
    cleanup_rig: CleanupRig,
) -> None:
    capability_unmerge_world(cleanup_rig, policy="moved")

    run_capability_unmerge(cleanup_rig)

    assert cleanup_rig.graph.nodes[("Capability", ABSORBED)]["status"] == "active"
    assert not [k for k in cleanup_rig.graph.edges if k[0] == "MERGED_INTO"]
    assert _into(cleanup_rig, ABSORBED) == [
        ("COVERS", "pa_1"),
        ("MITIGATED_BY", "rp_1"),
        ("REQUIRES", "obl_1"),
        ("REQUIRES", "obl_2"),
    ]
    assert _into(cleanup_rig, SURVIVOR) == [("REQUIRES", "obl_2")]
    assert [(k[2], k[4]) for k in cleanup_rig.graph.edges if k[0] == "GOVERNED_BY"] == [
        (ABSORBED, POLICY)
    ]


def test_an_edge_the_survivor_gained_since_the_merge_is_neither_removed_nor_in_the_group(
    cleanup_rig: CleanupRig,
) -> None:
    capability_unmerge_world(cleanup_rig)
    cleanup_rig.seed_endpoint("Obligation", "obl_new")
    cleanup_rig.seed_edge(
        ("REQUIRES", "Obligation", "obl_new", "Capability", SURVIVOR), legacy=False
    )

    outcome = run_capability_unmerge(cleanup_rig)

    assert [e["source_id"] for e in outcome["survivor_added_edges"]] == ["obl_new"]  # type: ignore[attr-defined]
    [group] = cleanup_rig.hooked.groups
    assert all("obl_new" not in str(p) for p in group.primitives)
    assert ("REQUIRES", "obl_new") in _into(cleanup_rig, SURVIVOR)


# -- the guard: the old WHERE, in Python (AC-BI-003) ----------------------------------------------


def _holds_after(
    rig: CleanupRig, break_it: Callable[[CleanupRig], None], *, policy: str | None = None
) -> bool:
    """The planned write still holds on the graph as it is AFTER `break_it` changed it."""
    plan = unmerge_plan(rig, ABSORBED)
    assert isinstance(plan, CapabilityUnmergePlan)
    break_it(rig)
    state = read_capability_unmerge_state(rig.read, inputs=plan.inputs)
    return capability_unmerge_guard_holds(
        state, plan.write, absorbed_id=ABSORBED, survivor_id=SURVIVOR
    )


def test_the_guard_holds_on_the_state_that_was_planned(cleanup_rig: CleanupRig) -> None:
    capability_unmerge_world(cleanup_rig, policy="moved")

    assert _holds_after(cleanup_rig, lambda _rig: None) is True


def _not_a_tombstone(rig: CleanupRig) -> None:
    rig.graph.nodes[("Capability", ABSORBED)]["status"] = "active"


def _redirect_gone(rig: CleanupRig) -> None:
    del rig.graph.edges[next(k for k in rig.graph.edges if k[0] == "MERGED_INTO")]


def _redirected_elsewhere(rig: CleanupRig) -> None:
    _redirect_gone(rig)
    rig.seed_capability("cap_winner", status="active")
    rig.seed_edge(("MERGED_INTO", "Capability", ABSORBED, "Capability", "cap_winner"), legacy=False)


def _survivor_merged(rig: CleanupRig) -> None:
    rig.graph.nodes[("Capability", SURVIVOR)]["status"] = "merged"


def _survivor_gone(rig: CleanupRig) -> None:
    del rig.graph.nodes[("Capability", SURVIVOR)]


def _absorbed_gone(rig: CleanupRig) -> None:
    del rig.graph.nodes[("Capability", ABSORBED)]


def _restore_endpoint_gone(rig: CleanupRig) -> None:
    del rig.graph.nodes[("Obligation", "obl_1")]


def _area_gone(rig: CleanupRig) -> None:
    del rig.graph.nodes[("PracticeArea", "pa_1")]


def _risk_path_gone(rig: CleanupRig) -> None:
    del rig.graph.nodes[("RiskPath", "rp_1")]


def _remove_edge_gone(rig: CleanupRig) -> None:
    del rig.graph.edges[
        next(k for k in rig.graph.edges if k[:3] == ("REQUIRES", "Obligation", "obl_1"))
    ]


def _policy_gone(rig: CleanupRig) -> None:
    del rig.graph.nodes[("Policy", POLICY)]


def _survivor_governed_edge_gone(rig: CleanupRig) -> None:
    del rig.graph.edges[next(k for k in rig.graph.edges if k[0] == "GOVERNED_BY")]


@pytest.mark.parametrize(
    "break_it",
    [
        _not_a_tombstone,
        _redirect_gone,
        _redirected_elsewhere,
        _survivor_merged,
        _survivor_gone,
        _absorbed_gone,
        _restore_endpoint_gone,
        _area_gone,
        _risk_path_gone,
        _remove_edge_gone,
    ],
)
def test_the_guard_is_false_when_the_tombstone_the_survivor_an_endpoint_or_an_edge_changed(
    cleanup_rig: CleanupRig, break_it: Callable[[CleanupRig], None]
) -> None:
    capability_unmerge_world(cleanup_rig)

    assert _holds_after(cleanup_rig, break_it) is False


@pytest.mark.parametrize("break_it", [_policy_gone, _survivor_governed_edge_gone])
def test_the_guard_is_false_when_the_restored_policy_or_the_moved_policy_edge_is_gone(
    cleanup_rig: CleanupRig, break_it: Callable[[CleanupRig], None]
) -> None:
    capability_unmerge_world(cleanup_rig, policy="moved")

    assert _holds_after(cleanup_rig, break_it) is False


def test_the_survivors_policy_edge_is_not_required_when_the_merge_did_not_move_it(
    cleanup_rig: CleanupRig,
) -> None:
    capability_unmerge_world(cleanup_rig, policy="kept")

    assert _holds_after(cleanup_rig, _survivor_governed_edge_gone) is True


@pytest.mark.parametrize("break_it", [_remove_edge_gone, _restore_endpoint_gone])
def test_a_guard_miss_between_the_replan_and_the_write_logs_nothing(
    cleanup_rig: CleanupRig, break_it: Callable[[CleanupRig], None]
) -> None:
    capability_unmerge_world(cleanup_rig)
    row = unmerge_row(cleanup_rig, ABSORBED)
    cleanup_rig.read.flip_before(_NODE_READ, 2, lambda: break_it(cleanup_rig))

    outcome = run_capability_unmerge(cleanup_rig, row=row)

    assert "changed" in str(outcome["error"])
    assert _rows(cleanup_rig) == [
        ("capability.unmerge", "applied"),
        ("capability.unmerge", "failed"),
    ]
    assert cleanup_rig.audit.rows[1].details["reason_code"] == "graph_guard_missed"
    assert cleanup_rig.logged_groups == []
    assert cleanup_rig.graph.nodes[("Capability", ABSORBED)]["status"] == "merged"


def test_an_unrelated_write_between_the_position_read_and_the_append_is_refused(
    cleanup_rig: CleanupRig,
) -> None:
    capability_unmerge_world(cleanup_rig)
    cleanup_rig.hooked.before_submit(cleanup_rig.append_unrelated_group)

    outcome = run_capability_unmerge(cleanup_rig)

    assert "changed" in str(outcome["error"])
    assert cleanup_rig.audit.rows[1].details["reason_code"] == "graph_guard_missed"
    assert len(cleanup_rig.logged_groups) == 1  # only the unrelated group
    assert cleanup_rig.graph.nodes[("Capability", ABSORBED)]["status"] == "merged"


# -- legacy identity-less edges (AC-BI-005, F4) ---------------------------------------------------


def test_legacy_edges_are_stamped_first_so_no_parallel_edge_results(
    cleanup_rig: CleanupRig,
) -> None:
    capability_unmerge_world(cleanup_rig, policy="moved", legacy=True)

    outcome = run_capability_unmerge(cleanup_rig)

    assert outcome["unmerged"] is True
    [event_id] = cleanup_rig.audit.event_ids
    links = [cleanup_rig.store.links[g.group_id] for g in cleanup_rig.logged_groups]
    assert links == [event_id, event_id]  # the stamp group, then the unmerge group
    keys = [k[:5] for k in cleanup_rig.graph.edges]
    assert len(keys) == len(set(keys))
    assert all(k[5] for k in cleanup_rig.graph.edges)


def test_a_refused_position_may_leave_the_legacy_stamp_in_the_log_and_no_unmerge_group(
    cleanup_rig: CleanupRig,
) -> None:
    capability_unmerge_world(cleanup_rig, legacy=True)
    cleanup_rig.hooked.before_submit(cleanup_rig.append_unrelated_group)

    outcome = run_capability_unmerge(cleanup_rig)

    assert "changed" in str(outcome["error"])
    event_id = cleanup_rig.audit.event_ids[0]
    links = [cleanup_rig.store.links[g.group_id] for g in cleanup_rig.logged_groups]
    assert links == [event_id, None]  # one stamp under the applied row; the rest is unrelated
    assert cleanup_rig.graph.nodes[("Capability", ABSORBED)]["status"] == "merged"


# -- the failure matrix (AC-BI-008) ---------------------------------------------------------------


def _reject_the_group(rig: CleanupRig) -> None:
    def _raise() -> None:
        message = "rejected by host 10.1.2.3 port 6379"
        raise GraphWriteRejectedError(message)

    rig.hooked.before_submit(_raise)


def _log_unavailable(rig: CleanupRig) -> None:
    rig.store.fail_appends()


def _graph_unreachable(rig: CleanupRig) -> None:
    def _raise() -> None:
        raise GraphUnavailableError

    rig.hooked.before_submit(_raise)


@pytest.mark.parametrize(
    "fail",
    [_reject_the_group, _log_unavailable, _graph_unreachable],
    ids=["rejected", "log-down", "unavailable"],
)
def test_a_gateway_failure_is_a_failed_row_and_the_generic_write_failed_error(
    cleanup_rig: CleanupRig, fail: Callable[[CleanupRig], None]
) -> None:
    capability_unmerge_world(cleanup_rig)
    fail(cleanup_rig)

    outcome = run_capability_unmerge(cleanup_rig)

    assert outcome == {"error": _WRITE_FAILED}
    assert _rows(cleanup_rig) == [
        ("capability.unmerge", "applied"),
        ("capability.unmerge", "failed"),
    ]
    assert cleanup_rig.audit.rows[1].details["reason_code"] == "graph_write_failed"
    assert "10.1.2.3" not in str(cleanup_rig.audit.rows[1].details)
    assert cleanup_rig.graph.nodes[("Capability", ABSORBED)]["status"] == "merged"


def test_no_installed_gateway_is_a_failed_row_and_the_generic_write_failed_error(
    cleanup_rig: CleanupRig,
) -> None:
    capability_unmerge_world(cleanup_rig)

    def _none(_config: ServiceConfig) -> object:
        raise GraphWriteUnavailableError("graph writes are unavailable: no gateway")

    dependencies = GraphCleanupDependencies(
        open_single_tenant_graph=lambda _config: cleanup_rig.read,
        audit_store=lambda _config: cleanup_rig.audit,
        access_role_store=lambda _config: roles(),
        graph_write_gateway=_none,  # pyright: ignore[reportArgumentType]
    )

    outcome = run_capability_unmerge(cleanup_rig, dependencies=dependencies)

    assert outcome == {"error": _WRITE_FAILED}
    assert cleanup_rig.audit.rows[1].details["reason_code"] == "graph_write_failed"


def test_a_group_committed_but_not_applied_is_a_success_with_the_survivor_added_edges(
    cleanup_rig: CleanupRig,
) -> None:
    capability_unmerge_world(cleanup_rig)
    cleanup_rig.seed_endpoint("Obligation", "obl_new")
    cleanup_rig.seed_edge(
        ("REQUIRES", "Obligation", "obl_new", "Capability", SURVIVOR), legacy=False
    )
    cleanup_rig.graph.fail_on_write(
        redis.exceptions.ConnectionError("Error connecting to 10.1.2.3:6379")
    )

    outcome = run_capability_unmerge(cleanup_rig)

    assert outcome["unmerged"] is True
    assert [e["source_id"] for e in outcome["survivor_added_edges"]] == ["obl_new"]  # type: ignore[attr-defined]
    assert _rows(cleanup_rig) == [("capability.unmerge", "applied")]
    assert len(cleanup_rig.hooked.groups) == 1
    assert "10.1.2.3" not in str(outcome)


def test_the_next_approval_while_the_graph_is_behind_its_log_writes_nothing(
    cleanup_rig: CleanupRig,
) -> None:
    capability_unmerge_world(cleanup_rig)
    cleanup_rig.graph.fail_on_write(redis.exceptions.ConnectionError("down"))
    assert run_capability_unmerge(cleanup_rig)["unmerged"] is True
    groups_before = len(cleanup_rig.logged_groups)

    second = run_capability_unmerge(cleanup_rig)

    assert second == {"error": _WRITE_FAILED}
    assert len(cleanup_rig.logged_groups) == groups_before
