"""`merge-capabilities` writes through the Graph Write Gateway.

One approved merge is one gateway group: the absorbed Capability's edges move onto the survivor
(an upsert per edge, a delete per old one), the absorbed node becomes a `merged` tombstone with a
`MERGED_INTO` edge. The group is linked to the `applied` audit row and guarded by the log position
read BEFORE the guard reads. The guard is the statement's `WHERE` condition,
evaluated in Python on a fresh read, for each of the four governance cases.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
import redis.exceptions

from graph_cleanup._fakes import ABSORBED, SURVIVOR
from graph_cleanup._gateway_fakes import POLICY, POLICY_SYSTEM, CleanupRig
from graph_cleanup._merge_runs import (
    capability_plan,
    capability_row,
    roles,
    run_capability_merge,
)
from ps_service.api.errors import GraphWriteUnavailableError
from ps_service.graph_cleanup.dependencies import GraphCleanupDependencies
from ps_service.graph_cleanup.graph_reader import read_capability_merge_state
from ps_service.graph_cleanup.group_builders import (
    build_capability_merge_group,
    capability_merge_guard_holds,
)
from ps_service.graph_gateway.errors import GraphUnavailableError, GraphWriteRejectedError
from ps_service.graph_gateway.models import (
    DeleteEdge,
    ExpectedPosition,
    MergeProperty,
    NodeRef,
    Primitive,
    UpsertEdge,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from ps_service.config import ServiceConfig

_NODE_READ = "RETURN c.id, c.name"
"""A distinctive part of the first read of the merge state: its first run (after the approval row
is built) is the replan, the second the guard."""
_WRITE_FAILED = (
    "the merge could not be completed; check its status with check-cleanup-approval before retrying"
)


def _cap(capability_id: str) -> NodeRef:
    return NodeRef(label="Capability", id=capability_id)


def _edge(edge_type: str, source: NodeRef, target: NodeRef) -> tuple[str, str, NodeRef, NodeRef]:
    return edge_type, f"{edge_type}:{source.id}->{target.id}", source, target


def _upsert(edge_type: str, source: NodeRef, target: NodeRef) -> UpsertEdge:
    kind, identity, src, tgt = _edge(edge_type, source, target)
    return UpsertEdge(type=kind, identity=identity, source=src, target=tgt)


def _delete(edge_type: str, source: NodeRef, target: NodeRef) -> DeleteEdge:
    kind, identity, src, tgt = _edge(edge_type, source, target)
    return DeleteEdge(type=kind, identity=identity, source=src, target=tgt)


def _obl(obligation_id: str) -> NodeRef:
    return NodeRef(label="Obligation", id=obligation_id)


_AREA = NodeRef(label="PracticeArea", id="pa_1")
_POLICY = NodeRef(label="Policy", id=POLICY)

_MOVES_IN = (
    _upsert("REQUIRES", _obl("obl_1"), _cap(SURVIVOR)),
    _upsert("REQUIRES", _obl("obl_2"), _cap(SURVIVOR)),
    _upsert("COVERS", _AREA, _cap(SURVIVOR)),
)
_MOVES_OUT = (
    _delete("REQUIRES", _obl("obl_1"), _cap(ABSORBED)),
    _delete("REQUIRES", _obl("obl_2"), _cap(ABSORBED)),
    _delete("COVERS", _AREA, _cap(ABSORBED)),
)
_TOMBSTONE = (
    MergeProperty(label="Capability", id=ABSORBED, properties={"status": "merged"}),
    _upsert("MERGED_INTO", _cap(ABSORBED), _cap(SURVIVOR)),
)
_CASES: dict[str, tuple[tuple[tuple[str, str, str], ...], tuple[Primitive, ...]]] = {
    "case1-neither-governed": ((), (*_MOVES_IN, *_MOVES_OUT, *_TOMBSTONE)),
    "case2-absorbed-governed": (
        ((ABSORBED, POLICY, "approved"),),
        (
            *_MOVES_IN,
            _upsert("GOVERNED_BY", _cap(SURVIVOR), _POLICY),
            *_MOVES_OUT,
            _delete("GOVERNED_BY", _cap(ABSORBED), _POLICY),
            *_TOMBSTONE,
        ),
    ),
    "case2-survivor-governed": (
        ((SURVIVOR, POLICY, "draft"),),
        (*_MOVES_IN, *_MOVES_OUT, *_TOMBSTONE),
    ),
    "case3-same-policy": (
        ((SURVIVOR, POLICY, "approved"), (ABSORBED, POLICY, "approved")),
        (*_MOVES_IN, *_MOVES_OUT, _delete("GOVERNED_BY", _cap(ABSORBED), _POLICY), *_TOMBSTONE),
    ),
}


def _acknowledged(case: str) -> bool:
    return case.startswith("case2")


def _world(rig: CleanupRig, case: str = "case1-neither-governed", *, legacy: bool = False) -> None:
    rig.seed_capability_merge_world(governed=_CASES[case][0], legacy=legacy)


def _rows(rig: CleanupRig) -> list[tuple[str, str]]:
    return [(r.action, r.outcome) for r in rig.audit.rows]


# -- the group -------------------------------------------------------------


@pytest.mark.parametrize("case", sorted(_CASES))
def test_one_merge_is_exactly_one_group_with_the_ordered_primitives_of_its_case(
    cleanup_rig: CleanupRig, case: str
) -> None:
    _world(cleanup_rig, case)
    position = cleanup_rig.store.last_position(POLICY_SYSTEM)

    outcome = run_capability_merge(cleanup_rig, acknowledged=_acknowledged(case))

    assert outcome == {"survivor_id": SURVIVOR, "absorbed_id": ABSORBED, "merged": True}
    [group] = cleanup_rig.hooked.groups
    assert group.graph == POLICY_SYSTEM
    assert group.primitives == _CASES[case][1]
    assert group.preconditions == (ExpectedPosition(position=position),)


@pytest.mark.parametrize("case", sorted(_CASES))
def test_the_pure_build_names_the_same_primitives(cleanup_rig: CleanupRig, case: str) -> None:
    _world(cleanup_rig, case)
    state = read_capability_merge_state(
        cleanup_rig.read, survivor_id=SURVIVOR, absorbed_id=ABSORBED
    )

    assert (
        build_capability_merge_group(state, survivor_id=SURVIVOR, absorbed_id=ABSORBED)
        == (_CASES[case][1])
    )


def test_the_groups_audit_event_id_is_the_applied_rows_id(cleanup_rig: CleanupRig) -> None:
    _world(cleanup_rig)

    run_capability_merge(cleanup_rig)

    [event_id] = cleanup_rig.audit.event_ids
    [group] = cleanup_rig.hooked.groups
    assert group.audit_event_id == event_id
    assert _rows(cleanup_rig) == [("capability.merge", "applied")]
    assert cleanup_rig.order() == ["audit:applied", "log_append"]


def test_the_position_is_read_before_the_guard_reads_and_the_guard_before_the_submit(
    cleanup_rig: CleanupRig,
) -> None:
    _world(cleanup_rig)
    row = capability_row(cleanup_rig)
    calls = cleanup_rig.hooked.calls
    cleanup_rig.read.flip_before(_NODE_READ, 2, lambda: calls.append("guard_read"))

    run_capability_merge(cleanup_rig, row=row)

    assert calls == [
        "is_caught_up",
        "read_log_position",
        "guard_read",
        "is_caught_up",  # the stamp needs a caught-up graph, whatever the order
        "stamp_legacy_edge_identity",
        "submit_group",
    ]


@pytest.mark.parametrize("case", sorted(_CASES))
def test_the_graph_after_the_group_has_the_edges_moved_and_the_tombstone(
    cleanup_rig: CleanupRig, case: str
) -> None:
    _world(cleanup_rig, case)

    run_capability_merge(cleanup_rig, acknowledged=_acknowledged(case))

    graph = cleanup_rig.graph
    assert graph.nodes[("Capability", ABSORBED)]["status"] == "merged"
    assert {k[:5] for k in graph.edges if k[0] == "MERGED_INTO"} == {
        ("MERGED_INTO", "Capability", ABSORBED, "Capability", SURVIVOR)
    }
    assert not [k for k in graph.edges if k[4] == ABSORBED and k[0] != "MERGED_INTO"]
    assert {(k[0], k[2]) for k in graph.edges if k[4] == SURVIVOR and k[0] != "MERGED_INTO"} == {
        ("REQUIRES", "obl_1"),
        ("REQUIRES", "obl_2"),
        ("COVERS", "pa_1"),
    }


def test_an_endpoint_already_linked_to_the_survivor_collapses_into_its_edge(
    cleanup_rig: CleanupRig,
) -> None:
    _world(cleanup_rig)

    run_capability_merge(cleanup_rig)

    requires_obl_2 = [
        k
        for k in cleanup_rig.graph.edges
        if k[:5] == ("REQUIRES", "Obligation", "obl_2", "Capability", SURVIVOR)
    ]
    assert len(requires_obl_2) == 1


def test_an_absorbed_capability_with_no_edges_is_only_tombstoned(cleanup_rig: CleanupRig) -> None:
    _world(cleanup_rig)
    for key in [k for k in cleanup_rig.graph.edges if k[4] == ABSORBED]:
        del cleanup_rig.graph.edges[key]

    run_capability_merge(cleanup_rig)

    [group] = cleanup_rig.hooked.groups
    assert group.primitives == _TOMBSTONE


# -- the guard: the WHERE condition, in Python ----------------------------------------------


def _holds(rig: CleanupRig) -> bool:
    plan = capability_plan(rig)
    state = read_capability_merge_state(rig.read, survivor_id=SURVIVOR, absorbed_id=ABSORBED)
    return capability_merge_guard_holds(state, plan.expected, absorbed_id=ABSORBED)


def _holds_after(rig: CleanupRig, break_it: Callable[[CleanupRig], None]) -> bool:
    """The previewed counts hold on the graph as it is AFTER `break_it` changed it."""
    expected = capability_plan(rig).expected
    break_it(rig)
    state = read_capability_merge_state(rig.read, survivor_id=SURVIVOR, absorbed_id=ABSORBED)
    return capability_merge_guard_holds(state, expected, absorbed_id=ABSORBED)


@pytest.mark.parametrize("case", sorted(_CASES))
def test_the_guard_holds_on_the_state_that_was_previewed_in_every_case(
    cleanup_rig: CleanupRig, case: str
) -> None:
    _world(cleanup_rig, case)

    assert _holds(cleanup_rig) is True


def test_a_legacy_capability_without_a_status_counts_as_active(cleanup_rig: CleanupRig) -> None:
    _world(cleanup_rig)
    for capability in (SURVIVOR, ABSORBED):
        del cleanup_rig.graph.nodes[("Capability", capability)]["status"]

    assert _holds(cleanup_rig) is True


def _add_requirer(rig: CleanupRig) -> None:
    rig.seed_endpoint("Obligation", "obl_new")
    rig.seed_edge(("REQUIRES", "Obligation", "obl_new", "Capability", ABSORBED), legacy=False)


def _add_coverer(rig: CleanupRig) -> None:
    rig.seed_endpoint("PracticeArea", "pa_new")
    rig.seed_edge(("COVERS", "PracticeArea", "pa_new", "Capability", ABSORBED), legacy=False)


def _add_mitigator(rig: CleanupRig) -> None:
    rig.seed_endpoint("RiskPath", "rp_new")
    rig.seed_edge(("MITIGATED_BY", "RiskPath", "rp_new", "Capability", ABSORBED), legacy=False)


def _drop_a_requirer(rig: CleanupRig) -> None:
    del rig.graph.edges[
        next(k for k in rig.graph.edges if k[:3] == ("REQUIRES", "Obligation", "obl_1"))
    ]


def _survivor_tombstone(rig: CleanupRig) -> None:
    rig.graph.nodes[("Capability", SURVIVOR)]["status"] = "merged"


def _absorbed_deprecated(rig: CleanupRig) -> None:
    rig.graph.nodes[("Capability", ABSORBED)]["status"] = "deprecated"


def _absorbed_gone(rig: CleanupRig) -> None:
    del rig.graph.nodes[("Capability", ABSORBED)]


def _survivor_gone(rig: CleanupRig) -> None:
    del rig.graph.nodes[("Capability", SURVIVOR)]


@pytest.mark.parametrize(
    "break_it",
    [
        _add_requirer,
        _add_coverer,
        _add_mitigator,
        _drop_a_requirer,
        _survivor_tombstone,
        _absorbed_deprecated,
        _absorbed_gone,
        _survivor_gone,
    ],
)
def test_the_guard_is_false_when_a_node_its_status_or_an_edge_count_changed(
    cleanup_rig: CleanupRig, break_it: Callable[[CleanupRig], None]
) -> None:
    _world(cleanup_rig)

    assert _holds_after(cleanup_rig, break_it) is False


def _policy_status_changes(rig: CleanupRig) -> None:
    rig.graph.nodes[("Policy", POLICY)]["status"] = "deprecated"


def _ensure_policy(rig: CleanupRig) -> None:
    if ("Policy", POLICY) not in rig.graph.nodes:
        rig.seed_policy(POLICY, title="Incident Policy", status="approved")


def _absorbed_is_governed_now(rig: CleanupRig) -> None:
    _ensure_policy(rig)
    rig.seed_governs(ABSORBED, POLICY)


def _survivor_is_governed_now(rig: CleanupRig) -> None:
    _ensure_policy(rig)
    rig.seed_governs(SURVIVOR, POLICY)


def _absorbed_governed_edge_dropped(rig: CleanupRig) -> None:
    del rig.graph.edges[
        next(k for k in rig.graph.edges if k[:3] == ("GOVERNED_BY", "Capability", ABSORBED))
    ]


def _absorbed_swaps_policy(rig: CleanupRig) -> None:
    _absorbed_governed_edge_dropped(rig)
    rig.seed_policy("pol_2", title="Other", status="approved")
    rig.seed_governs(ABSORBED, "pol_2")


@pytest.mark.parametrize(
    ("case", "break_it"),
    [
        ("case1-neither-governed", _absorbed_is_governed_now),
        ("case1-neither-governed", _survivor_is_governed_now),
        ("case2-absorbed-governed", _policy_status_changes),
        ("case2-absorbed-governed", _survivor_is_governed_now),
        ("case2-absorbed-governed", _absorbed_governed_edge_dropped),
        ("case2-survivor-governed", _policy_status_changes),
        ("case2-survivor-governed", _absorbed_is_governed_now),
        ("case3-same-policy", _policy_status_changes),
        ("case3-same-policy", _absorbed_governed_edge_dropped),
        ("case3-same-policy", _absorbed_swaps_policy),
    ],
)
def test_the_guard_is_false_when_the_governance_of_a_side_changed(
    cleanup_rig: CleanupRig, case: str, break_it: Callable[[CleanupRig], None]
) -> None:
    _world(cleanup_rig, case)

    assert _holds_after(cleanup_rig, break_it) is False


def test_the_guard_is_false_when_the_sides_are_governed_by_different_policies(
    cleanup_rig: CleanupRig,
) -> None:
    _world(cleanup_rig, "case2-absorbed-governed")
    expected = capability_plan(cleanup_rig).expected
    cleanup_rig.seed_policy("pol_2", title="Other", status="approved")
    cleanup_rig.seed_governs(SURVIVOR, "pol_2")
    # pin every other predicate to the new state, so only the "different policies" one is left
    expected = expected.model_copy(
        update={
            "survivor_governed": 1,
            "survivor_policy_id": "pol_2",
            "survivor_policy_status": "approved",
        }
    )
    state = read_capability_merge_state(
        cleanup_rig.read, survivor_id=SURVIVOR, absorbed_id=ABSORBED
    )

    assert capability_merge_guard_holds(state, expected, absorbed_id=ABSORBED) is False


@pytest.mark.parametrize(
    "break_it",
    [_add_requirer, _survivor_tombstone, _policy_status_changes],
    ids=["edge-count", "tombstone", "policy-status"],
)
def test_a_guard_miss_between_the_replan_and_the_write_logs_nothing(
    cleanup_rig: CleanupRig, break_it: Callable[[CleanupRig], None]
) -> None:
    _world(cleanup_rig, "case2-absorbed-governed")
    row = capability_row(cleanup_rig, acknowledged=True)
    cleanup_rig.read.flip_before(_NODE_READ, 2, lambda: break_it(cleanup_rig))

    outcome = run_capability_merge(cleanup_rig, row=row)

    assert "changed" in str(outcome["error"])
    assert _rows(cleanup_rig) == [("capability.merge", "applied"), ("capability.merge", "failed")]
    assert cleanup_rig.audit.rows[1].details["reason_code"] == "graph_guard_missed"
    assert cleanup_rig.logged_groups == []


def test_an_unrelated_write_between_the_position_read_and_the_append_is_refused(
    cleanup_rig: CleanupRig,
) -> None:
    _world(cleanup_rig)
    cleanup_rig.hooked.before_submit(cleanup_rig.append_unrelated_group)

    outcome = run_capability_merge(cleanup_rig)

    assert "changed" in str(outcome["error"])
    assert cleanup_rig.audit.rows[1].details["reason_code"] == "graph_guard_missed"
    assert len(cleanup_rig.logged_groups) == 1  # only the unrelated group
    assert cleanup_rig.graph.nodes[("Capability", ABSORBED)]["status"] == "active"


# -- legacy identity-less edges ---------------------------------------------------


def test_legacy_edges_are_stamped_first_so_no_parallel_edge_results(
    cleanup_rig: CleanupRig,
) -> None:
    _world(cleanup_rig, "case2-absorbed-governed", legacy=True)

    outcome = run_capability_merge(cleanup_rig, acknowledged=True)

    assert outcome["merged"] is True
    [event_id] = cleanup_rig.audit.event_ids
    links = [cleanup_rig.store.links[g.group_id] for g in cleanup_rig.logged_groups]
    assert links == [event_id, event_id]  # the stamp group, then the merge group
    on_survivor = [k[:5] for k in cleanup_rig.graph.edges if k[4] == SURVIVOR]
    assert len(on_survivor) == len(set(on_survivor))  # no parallel edge
    assert all(k[5] for k in cleanup_rig.graph.edges)  # every edge now has an identity


def test_a_refused_position_may_leave_the_legacy_stamp_in_the_log_and_no_merge_group(
    cleanup_rig: CleanupRig,
) -> None:
    _world(cleanup_rig, legacy=True)
    cleanup_rig.hooked.before_submit(cleanup_rig.append_unrelated_group)

    outcome = run_capability_merge(cleanup_rig)

    assert "changed" in str(outcome["error"])
    event_id = cleanup_rig.audit.event_ids[0]
    links = [cleanup_rig.store.links[g.group_id] for g in cleanup_rig.logged_groups]
    assert links == [event_id, None]  # one stamp under the applied row; the rest is unrelated
    assert cleanup_rig.graph.nodes[("Capability", ABSORBED)]["status"] == "active"


# -- the failure matrix ---------------------------------------------------------------


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
    _world(cleanup_rig)
    fail(cleanup_rig)

    outcome = run_capability_merge(cleanup_rig)

    assert outcome == {"error": _WRITE_FAILED}
    assert _rows(cleanup_rig) == [("capability.merge", "applied"), ("capability.merge", "failed")]
    assert cleanup_rig.audit.rows[1].details["reason_code"] == "graph_write_failed"
    assert "10.1.2.3" not in str(cleanup_rig.audit.rows[1].details)


def test_no_installed_gateway_is_a_failed_row_and_the_generic_write_failed_error(
    cleanup_rig: CleanupRig,
) -> None:
    _world(cleanup_rig)

    def _none(_config: ServiceConfig) -> object:
        raise GraphWriteUnavailableError("graph writes are unavailable: no gateway")

    dependencies = GraphCleanupDependencies(
        open_single_tenant_graph=lambda _config: cleanup_rig.read,
        audit_store=lambda _config: cleanup_rig.audit,
        access_role_store=lambda _config: roles(),
        graph_write_gateway=_none,  # pyright: ignore[reportArgumentType]
    )

    outcome = run_capability_merge(cleanup_rig, dependencies=dependencies)

    assert outcome == {"error": _WRITE_FAILED}
    assert cleanup_rig.audit.rows[1].details["reason_code"] == "graph_write_failed"


def test_a_group_committed_but_not_applied_is_a_success_with_no_failed_row(
    cleanup_rig: CleanupRig,
) -> None:
    _world(cleanup_rig)
    cleanup_rig.graph.fail_on_write(
        redis.exceptions.ConnectionError("Error connecting to 10.1.2.3:6379")
    )

    outcome = run_capability_merge(cleanup_rig)

    assert outcome == {"survivor_id": SURVIVOR, "absorbed_id": ABSORBED, "merged": True}
    assert _rows(cleanup_rig) == [("capability.merge", "applied")]
    assert len(cleanup_rig.hooked.groups) == 1
    assert "10.1.2.3" not in str(outcome)


def test_the_next_approval_while_the_graph_is_behind_its_log_writes_nothing(
    cleanup_rig: CleanupRig,
) -> None:
    _world(cleanup_rig)
    cleanup_rig.graph.fail_on_write(redis.exceptions.ConnectionError("down"))
    assert run_capability_merge(cleanup_rig)["merged"] is True
    groups_before = len(cleanup_rig.logged_groups)

    second = run_capability_merge(cleanup_rig)

    assert second == {"error": _WRITE_FAILED}
    assert len(cleanup_rig.logged_groups) == groups_before


def test_an_unstorable_property_is_refused_as_a_write_failure_before_anything_is_logged(
    cleanup_rig: CleanupRig,
) -> None:
    """A node id the gateway cannot name (blank) fails its primitive, not the graph."""
    _world(cleanup_rig)
    cleanup_rig.seed_endpoint("Obligation", " ")
    cleanup_rig.seed_edge(("REQUIRES", "Obligation", " ", "Capability", ABSORBED), legacy=False)

    outcome = run_capability_merge(cleanup_rig)

    assert outcome == {"error": _WRITE_FAILED}
    assert cleanup_rig.logged_groups == []
