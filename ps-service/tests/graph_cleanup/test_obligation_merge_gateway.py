"""`merge-obligations` writes through the Graph Write Gateway.

One approved merge is one gateway group: the absorbed Obligation's `SATISFIED_BY` and `REQUIRES`
edges are unioned onto the survivor, a `MergedObligation` marker is upserted and the absorbed node
is deleted LAST, in the same group, so a delete without a redirect cannot be logged. The group is
linked to the `applied` audit row and guarded by the log position read BEFORE the guard reads
(guard, group, failure matrix). The guard is the old statement's `WHERE`, evaluated in Python.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
import redis.exceptions

from graph_cleanup._fakes import OBL_ABSORBED, OBL_SURVIVOR, ROLE_ID
from graph_cleanup._gateway_fakes import POLICY_SYSTEM, CleanupRig
from graph_cleanup._merge_runs import (
    obligation_plan,
    obligation_row,
    roles,
    run_obligation_merge,
)
from ps_service.api.errors import GraphWriteUnavailableError
from ps_service.graph_cleanup.dependencies import GraphCleanupDependencies
from ps_service.graph_cleanup.graph_reader import read_obligation_merge_state
from ps_service.graph_cleanup.group_builders import (
    build_obligation_merge_group,
    obligation_merge_guard_holds,
)
from ps_service.graph_gateway.errors import GraphUnavailableError, GraphWriteRejectedError
from ps_service.graph_gateway.label_allow_list import ALLOWED_NODE_LABELS
from ps_service.graph_gateway.models import (
    DeleteNode,
    ExpectedPosition,
    NodeRef,
    UpsertEdge,
    UpsertNode,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from ps_service.config import ServiceConfig

_NODE_READ = "RETURN o.id, o.text"
"""A distinctive part of the first read of the merge state: its first run (after the approval row
is built) is the replan, the second the guard."""
_WRITE_FAILED = (
    "the merge could not be completed; check its status with check-cleanup-approval before retrying"
)

_SURVIVOR = NodeRef(label="Obligation", id=OBL_SURVIVOR)
_CAP = NodeRef(label="Capability", id="cap_1")


def _requirement(requirement_id: str) -> NodeRef:
    return NodeRef(label="Requirement", id=requirement_id)


_PRIMITIVES = (
    UpsertEdge(
        type="SATISFIED_BY",
        identity=f"SATISFIED_BY:req_1->{OBL_SURVIVOR}",
        source=_requirement("req_1"),
        target=_SURVIVOR,
    ),
    UpsertEdge(
        type="SATISFIED_BY",
        identity=f"SATISFIED_BY:req_2->{OBL_SURVIVOR}",
        source=_requirement("req_2"),
        target=_SURVIVOR,
    ),
    UpsertEdge(
        type="REQUIRES",
        identity=f"REQUIRES:{OBL_SURVIVOR}->cap_1",
        source=_SURVIVOR,
        target=_CAP,
    ),
    UpsertNode(label="MergedObligation", id=OBL_ABSORBED, properties={"merged_into": OBL_SURVIVOR}),
    DeleteNode(label="Obligation", id=OBL_ABSORBED),
)


def _world(rig: CleanupRig, *, legacy: bool = False) -> None:
    rig.seed_obligation_merge_world(legacy=legacy)
    rig.seed_endpoint("Capability", "cap_new", name="cap_new", status="active")


def _rows(rig: CleanupRig) -> list[tuple[str, str]]:
    return [(r.action, r.outcome) for r in rig.audit.rows]


def _edges_of(rig: CleanupRig, node_id: str) -> list[tuple[str, str, str]]:
    return [(k[0], k[2], k[4]) for k in rig.graph.edges if node_id in {k[2], k[4]}]


# -- the group ------------------------------------------------------------------------------------


def test_one_merge_is_exactly_one_group_with_the_ordered_primitives_and_the_delete_last(
    cleanup_rig: CleanupRig,
) -> None:
    _world(cleanup_rig)
    position = cleanup_rig.store.last_position(POLICY_SYSTEM)

    outcome = run_obligation_merge(cleanup_rig)

    assert outcome == {"survivor_id": OBL_SURVIVOR, "absorbed_id": OBL_ABSORBED, "merged": True}
    [group] = cleanup_rig.hooked.groups
    assert group.graph == POLICY_SYSTEM
    assert group.primitives == _PRIMITIVES
    assert group.preconditions == (ExpectedPosition(position=position),)


def test_the_marker_is_upserted_before_the_delete_in_the_same_group() -> None:
    marker_index = next(i for i, p in enumerate(_PRIMITIVES) if isinstance(p, UpsertNode))
    delete_index = next(i for i, p in enumerate(_PRIMITIVES) if isinstance(p, DeleteNode))

    assert marker_index < delete_index == len(_PRIMITIVES) - 1


def test_the_pure_build_names_the_same_primitives(cleanup_rig: CleanupRig) -> None:
    _world(cleanup_rig)
    state = read_obligation_merge_state(
        cleanup_rig.read, survivor_id=OBL_SURVIVOR, absorbed_id=OBL_ABSORBED
    )

    assert (
        build_obligation_merge_group(state, survivor_id=OBL_SURVIVOR, absorbed_id=OBL_ABSORBED)
        == _PRIMITIVES
    )


def test_the_groups_audit_event_id_is_the_applied_rows_id(cleanup_rig: CleanupRig) -> None:
    _world(cleanup_rig)

    run_obligation_merge(cleanup_rig)

    [event_id] = cleanup_rig.audit.event_ids
    [group] = cleanup_rig.hooked.groups
    assert group.audit_event_id == event_id
    assert _rows(cleanup_rig) == [("obligation.merge", "applied")]
    assert cleanup_rig.order() == ["audit:applied", "log_append"]


def test_the_position_is_read_before_the_guard_reads_and_the_guard_before_the_submit(
    cleanup_rig: CleanupRig,
) -> None:
    _world(cleanup_rig)
    row = obligation_row(cleanup_rig)
    calls = cleanup_rig.hooked.calls
    cleanup_rig.read.flip_before(_NODE_READ, 2, lambda: calls.append("guard_read"))

    run_obligation_merge(cleanup_rig, row=row)

    assert calls == [
        "is_caught_up",
        "read_log_position",
        "guard_read",
        "is_caught_up",  # the stamp needs a caught-up graph, whatever the order
        "stamp_legacy_edge_identity",
        "submit_group",
    ]


def test_the_graph_after_the_group_has_the_union_the_marker_and_no_absorbed_node(
    cleanup_rig: CleanupRig,
) -> None:
    _world(cleanup_rig)

    run_obligation_merge(cleanup_rig)

    graph = cleanup_rig.graph
    assert ("Obligation", OBL_ABSORBED) not in graph.nodes
    assert graph.nodes[("MergedObligation", OBL_ABSORBED)] == {"merged_into": OBL_SURVIVOR}
    assert _edges_of(cleanup_rig, OBL_ABSORBED) == []  # every edge went with the node, HAS too
    assert sorted(_edges_of(cleanup_rig, OBL_SURVIVOR)) == [
        ("HAS", ROLE_ID, OBL_SURVIVOR),
        ("REQUIRES", OBL_SURVIVOR, "cap_1"),
        ("SATISFIED_BY", "req_1", OBL_SURVIVOR),
        ("SATISFIED_BY", "req_2", OBL_SURVIVOR),
    ]


def test_an_endpoint_already_linked_to_the_survivor_collapses_into_its_edge(
    cleanup_rig: CleanupRig,
) -> None:
    _world(cleanup_rig)

    run_obligation_merge(cleanup_rig)

    satisfied = [k for k in cleanup_rig.graph.edges if k[0] == "SATISFIED_BY" and k[2] == "req_2"]
    requires = [k for k in cleanup_rig.graph.edges if k[0] == "REQUIRES" and k[2] == OBL_SURVIVOR]
    assert len(satisfied) == 1
    assert len(requires) == 1


def test_an_absorbed_obligation_with_no_edges_gets_only_its_marker_and_its_delete(
    cleanup_rig: CleanupRig,
) -> None:
    _world(cleanup_rig)
    for key in [
        k for k in cleanup_rig.graph.edges if k[0] != "HAS" and OBL_ABSORBED in {k[2], k[4]}
    ]:
        del cleanup_rig.graph.edges[key]

    run_obligation_merge(cleanup_rig)

    [group] = cleanup_rig.hooked.groups
    assert group.primitives == _PRIMITIVES[3:]


def test_the_marker_label_is_one_the_gateway_allows() -> None:
    assert "MergedObligation" in ALLOWED_NODE_LABELS


# -- the guard: the old WHERE, in Python ----------------------------------------------------------


def _holds_after(rig: CleanupRig, break_it: Callable[[CleanupRig], None]) -> bool:
    """The previewed counts hold on the graph as it is AFTER `break_it` changed it."""
    expected = obligation_plan(rig).expected
    break_it(rig)
    state = read_obligation_merge_state(
        rig.read, survivor_id=OBL_SURVIVOR, absorbed_id=OBL_ABSORBED
    )
    return obligation_merge_guard_holds(
        state, expected, survivor_id=OBL_SURVIVOR, absorbed_id=OBL_ABSORBED
    )


def test_the_guard_holds_on_the_state_that_was_previewed(cleanup_rig: CleanupRig) -> None:
    _world(cleanup_rig)

    assert _holds_after(cleanup_rig, lambda _rig: None) is True


def _new_requirement(rig: CleanupRig) -> None:
    rig.seed_endpoint("Requirement", "req_new")
    rig.seed_edge(
        ("SATISFIED_BY", "Requirement", "req_new", "Obligation", OBL_ABSORBED), legacy=False
    )


def _lost_a_requirement(rig: CleanupRig) -> None:
    del rig.graph.edges[
        next(
            k
            for k in rig.graph.edges
            if k[:5] == ("SATISFIED_BY", "Requirement", "req_1", "Obligation", OBL_ABSORBED)
        )
    ]


def _new_capability(rig: CleanupRig) -> None:
    rig.seed_edge(("REQUIRES", "Obligation", OBL_ABSORBED, "Capability", "cap_new"), legacy=False)


def _lost_the_capability(rig: CleanupRig) -> None:
    del rig.graph.edges[
        next(k for k in rig.graph.edges if k[:3] == ("REQUIRES", "Obligation", OBL_ABSORBED))
    ]


def _absorbed_gone(rig: CleanupRig) -> None:
    del rig.graph.nodes[("Obligation", OBL_ABSORBED)]


def _survivor_gone(rig: CleanupRig) -> None:
    del rig.graph.nodes[("Obligation", OBL_SURVIVOR)]


def _absorbed_moves_to_another_role(rig: CleanupRig) -> None:
    del rig.graph.edges[
        next(
            k
            for k in rig.graph.edges
            if k[:5] == ("HAS", "Role", ROLE_ID, "Obligation", OBL_ABSORBED)
        )
    ]
    rig.seed_endpoint("Role", "role_2", name="Importer")
    rig.seed_edge(("HAS", "Role", "role_2", "Obligation", OBL_ABSORBED), legacy=False)


def _absorbed_has_no_role(rig: CleanupRig) -> None:
    del rig.graph.edges[
        next(
            k
            for k in rig.graph.edges
            if k[:5] == ("HAS", "Role", ROLE_ID, "Obligation", OBL_ABSORBED)
        )
    ]


@pytest.mark.parametrize(
    "break_it",
    [
        _new_requirement,
        _lost_a_requirement,
        _new_capability,
        _lost_the_capability,
        _absorbed_gone,
        _survivor_gone,
        _absorbed_moves_to_another_role,
        _absorbed_has_no_role,
    ],
)
def test_the_guard_is_false_when_a_node_the_shared_role_or_an_edge_count_changed(
    cleanup_rig: CleanupRig, break_it: Callable[[CleanupRig], None]
) -> None:
    _world(cleanup_rig)

    assert _holds_after(cleanup_rig, break_it) is False


def test_the_guard_is_false_for_a_merge_into_itself(cleanup_rig: CleanupRig) -> None:
    _world(cleanup_rig)
    state = read_obligation_merge_state(
        cleanup_rig.read, survivor_id=OBL_SURVIVOR, absorbed_id=OBL_SURVIVOR
    )

    assert not obligation_merge_guard_holds(
        state,
        obligation_plan(cleanup_rig).expected,
        survivor_id=OBL_SURVIVOR,
        absorbed_id=OBL_SURVIVOR,
    )


@pytest.mark.parametrize(
    "break_it",
    [_new_requirement, _new_capability, _absorbed_moves_to_another_role],
    ids=["satisfied-count", "requires-count", "different-role"],
)
def test_a_guard_miss_between_the_replan_and_the_write_logs_nothing(
    cleanup_rig: CleanupRig, break_it: Callable[[CleanupRig], None]
) -> None:
    _world(cleanup_rig)
    row = obligation_row(cleanup_rig)
    cleanup_rig.read.flip_before(_NODE_READ, 2, lambda: break_it(cleanup_rig))

    outcome = run_obligation_merge(cleanup_rig, row=row)

    assert "changed" in str(outcome["error"])
    assert _rows(cleanup_rig) == [("obligation.merge", "applied"), ("obligation.merge", "failed")]
    assert cleanup_rig.audit.rows[1].details["reason_code"] == "graph_guard_missed"
    assert cleanup_rig.logged_groups == []
    assert ("Obligation", OBL_ABSORBED) in cleanup_rig.graph.nodes


def test_an_unrelated_write_between_the_position_read_and_the_append_is_refused(
    cleanup_rig: CleanupRig,
) -> None:
    _world(cleanup_rig)
    cleanup_rig.hooked.before_submit(cleanup_rig.append_unrelated_group)

    outcome = run_obligation_merge(cleanup_rig)

    assert "changed" in str(outcome["error"])
    assert cleanup_rig.audit.rows[1].details["reason_code"] == "graph_guard_missed"
    assert len(cleanup_rig.logged_groups) == 1  # only the unrelated group
    assert ("Obligation", OBL_ABSORBED) in cleanup_rig.graph.nodes
    assert ("MergedObligation", OBL_ABSORBED) not in cleanup_rig.graph.nodes


# -- legacy identity-less edges -------------------------------------------------------------------


def test_legacy_edges_are_stamped_first_so_no_parallel_edge_results(
    cleanup_rig: CleanupRig,
) -> None:
    _world(cleanup_rig, legacy=True)

    outcome = run_obligation_merge(cleanup_rig)

    assert outcome["merged"] is True
    [event_id] = cleanup_rig.audit.event_ids
    links = [cleanup_rig.store.links[g.group_id] for g in cleanup_rig.logged_groups]
    assert links == [event_id, event_id]  # the stamp group, then the merge group
    on_survivor = [k[:5] for k in cleanup_rig.graph.edges if OBL_SURVIVOR in {k[2], k[4]}]
    assert len(on_survivor) == len(set(on_survivor))
    assert all(k[5] for k in cleanup_rig.graph.edges)


def test_a_refused_position_may_leave_the_legacy_stamp_in_the_log_and_no_merge_group(
    cleanup_rig: CleanupRig,
) -> None:
    _world(cleanup_rig, legacy=True)
    cleanup_rig.hooked.before_submit(cleanup_rig.append_unrelated_group)

    outcome = run_obligation_merge(cleanup_rig)

    assert "changed" in str(outcome["error"])
    event_id = cleanup_rig.audit.event_ids[0]
    links = [cleanup_rig.store.links[g.group_id] for g in cleanup_rig.logged_groups]
    assert links == [event_id, None]  # one stamp under the applied row; the rest is unrelated
    assert ("Obligation", OBL_ABSORBED) in cleanup_rig.graph.nodes


# -- the failure matrix ---------------------------------------------------------------------------


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

    outcome = run_obligation_merge(cleanup_rig)

    assert outcome == {"error": _WRITE_FAILED}
    assert _rows(cleanup_rig) == [("obligation.merge", "applied"), ("obligation.merge", "failed")]
    assert cleanup_rig.audit.rows[1].details["reason_code"] == "graph_write_failed"
    assert "10.1.2.3" not in str(cleanup_rig.audit.rows[1].details)
    assert ("Obligation", OBL_ABSORBED) in cleanup_rig.graph.nodes


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

    outcome = run_obligation_merge(cleanup_rig, dependencies=dependencies)

    assert outcome == {"error": _WRITE_FAILED}
    assert cleanup_rig.audit.rows[1].details["reason_code"] == "graph_write_failed"


def test_a_group_committed_but_not_applied_is_a_success_with_no_failed_row(
    cleanup_rig: CleanupRig,
) -> None:
    _world(cleanup_rig)
    cleanup_rig.graph.fail_on_write(
        redis.exceptions.ConnectionError("Error connecting to 10.1.2.3:6379")
    )

    outcome = run_obligation_merge(cleanup_rig)

    assert outcome == {"survivor_id": OBL_SURVIVOR, "absorbed_id": OBL_ABSORBED, "merged": True}
    assert _rows(cleanup_rig) == [("obligation.merge", "applied")]
    assert len(cleanup_rig.hooked.groups) == 1
    assert "10.1.2.3" not in str(outcome)


def test_the_next_approval_while_the_graph_is_behind_its_log_writes_nothing(
    cleanup_rig: CleanupRig,
) -> None:
    _world(cleanup_rig)
    cleanup_rig.graph.fail_on_write(redis.exceptions.ConnectionError("down"))
    assert run_obligation_merge(cleanup_rig)["merged"] is True
    groups_before = len(cleanup_rig.logged_groups)

    second = run_obligation_merge(cleanup_rig)

    assert second == {"error": _WRITE_FAILED}
    assert len(cleanup_rig.logged_groups) == groups_before
