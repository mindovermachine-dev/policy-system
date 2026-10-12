"""`unmerge` of an Obligation writes through the Graph Write Gateway (issue #212, slice S6).

One approved unmerge is one gateway group: the deleted Obligation is recreated by an idempotent
upsert by id (AC-BI-004) with its `HAS`, `SATISFIED_BY` and `REQUIRES` edges, and the
`MergedObligation` marker is deleted in the same group, so a recreate without the marker removal
(or the reverse) cannot be logged. The group is linked to the `applied` audit row and guarded by
the log position read BEFORE the guard reads (AC-BI-003/005/006/008). The guard is the old
statement's `WHERE`, evaluated in Python.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
import redis.exceptions

from graph_cleanup._fakes import OBL_ABSORBED, OBL_SURVIVOR, ROLE_ID
from graph_cleanup._gateway_fakes import POLICY_SYSTEM, CleanupRig
from graph_cleanup._merge_runs import CONFIG, roles
from graph_cleanup._unmerge_runs import (
    obligation_unmerge_world,
    run_obligation_unmerge,
    unmerge_plan,
    unmerge_row,
)
from ps_service.api.errors import GraphWriteUnavailableError
from ps_service.graph_cleanup.dependencies import GraphCleanupDependencies
from ps_service.graph_cleanup.errors import GraphCleanupPersistenceError
from ps_service.graph_cleanup.graph_reader import read_obligation_unmerge_state
from ps_service.graph_cleanup.graph_submission import graph_write_target
from ps_service.graph_cleanup.group_builders import (
    build_obligation_unmerge_group,
    obligation_unmerge_guard_holds,
    write_obligation_unmerge,
)
from ps_service.graph_cleanup.models import ObligationUnmergePlan
from ps_service.graph_gateway.errors import GraphUnavailableError, GraphWriteRejectedError
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
"""A distinctive part of the first read of the unmerge state: its first run (when the approval row
is built) is the replan, the second the guard."""
_WRITE_FAILED = (
    "the unmerge could not be completed; check its status with check-cleanup-approval before "
    "retrying"
)

_ABSORBED = NodeRef(label="Obligation", id=OBL_ABSORBED)
_PROPERTIES: dict[str, object] = {"confidence": 0.8, "text": "Report  incidents."}


def _satisfied(requirement_id: str) -> UpsertEdge:
    return UpsertEdge(
        type="SATISFIED_BY",
        identity=f"SATISFIED_BY:{requirement_id}->{OBL_ABSORBED}",
        source=NodeRef(label="Requirement", id=requirement_id),
        target=_ABSORBED,
    )


_PRIMITIVES = (
    UpsertNode(label="Obligation", id=OBL_ABSORBED, properties=_PROPERTIES),
    UpsertEdge(
        type="HAS",
        identity=f"HAS:{ROLE_ID}->{OBL_ABSORBED}",
        source=NodeRef(label="Role", id=ROLE_ID),
        target=_ABSORBED,
    ),
    _satisfied("req_1"),
    _satisfied("req_2"),
    UpsertEdge(
        type="REQUIRES",
        identity=f"REQUIRES:{OBL_ABSORBED}->cap_1",
        source=_ABSORBED,
        target=NodeRef(label="Capability", id="cap_1"),
    ),
    DeleteNode(label="MergedObligation", id=OBL_ABSORBED),
)


def _rows(rig: CleanupRig) -> list[tuple[str, str]]:
    return [(r.action, r.outcome) for r in rig.audit.rows]


def _edges_of(rig: CleanupRig, node_id: str) -> list[tuple[str, str, str]]:
    return sorted((k[0], k[2], k[4]) for k in rig.graph.edges if node_id in {k[2], k[4]})


# -- the group (AC-BI-004, AC-BI-005, AC-BI-006) --------------------------------------------------


def test_one_unmerge_is_exactly_one_group_with_the_recreate_the_edges_and_the_marker_removal(
    cleanup_rig: CleanupRig,
) -> None:
    obligation_unmerge_world(cleanup_rig)
    position = cleanup_rig.store.last_position(POLICY_SYSTEM)

    outcome = run_obligation_unmerge(cleanup_rig)

    assert outcome == {
        "merged_id": OBL_ABSORBED,
        "survivor_id": OBL_SURVIVOR,
        "unmerged": True,
        "survivor_added_edges": [],
    }
    [group] = cleanup_rig.hooked.groups
    assert group.graph == POLICY_SYSTEM
    assert group.primitives == _PRIMITIVES
    assert group.preconditions == (ExpectedPosition(position=position),)


def test_the_recreate_is_an_upsert_by_id_and_the_marker_removal_follows_it_in_the_same_group() -> (
    None
):
    upsert = next(i for i, p in enumerate(_PRIMITIVES) if isinstance(p, UpsertNode))
    removal = next(i for i, p in enumerate(_PRIMITIVES) if isinstance(p, DeleteNode))

    assert upsert == 0
    assert removal == len(_PRIMITIVES) - 1


def test_the_pure_build_names_the_same_primitives(cleanup_rig: CleanupRig) -> None:
    obligation_unmerge_world(cleanup_rig)
    plan = unmerge_plan(cleanup_rig, OBL_ABSORBED)
    assert isinstance(plan, ObligationUnmergePlan)

    assert build_obligation_unmerge_group(plan.write, absorbed_id=OBL_ABSORBED) == _PRIMITIVES


def test_the_groups_audit_event_id_is_the_applied_rows_id(cleanup_rig: CleanupRig) -> None:
    obligation_unmerge_world(cleanup_rig)

    run_obligation_unmerge(cleanup_rig)

    [event_id] = cleanup_rig.audit.event_ids
    [group] = cleanup_rig.hooked.groups
    assert group.audit_event_id == event_id
    assert _rows(cleanup_rig) == [("obligation.unmerge", "applied")]
    assert cleanup_rig.order() == ["audit:applied", "log_append"]


def test_the_position_is_read_before_the_guard_reads_and_the_guard_before_the_submit(
    cleanup_rig: CleanupRig,
) -> None:
    obligation_unmerge_world(cleanup_rig)
    row = unmerge_row(cleanup_rig, OBL_ABSORBED)
    calls = cleanup_rig.hooked.calls
    cleanup_rig.read.flip_before(_NODE_READ, 2, lambda: calls.append("guard_read"))

    run_obligation_unmerge(cleanup_rig, row=row)

    assert calls == [
        "is_caught_up",
        "read_log_position",
        "guard_read",
        "is_caught_up",  # the stamp needs a caught-up graph, whatever the order
        "stamp_legacy_edge_identity",
        "submit_group",
    ]


def test_the_graph_after_the_group_has_the_obligation_its_edges_and_no_marker(
    cleanup_rig: CleanupRig,
) -> None:
    obligation_unmerge_world(cleanup_rig)

    run_obligation_unmerge(cleanup_rig)

    graph = cleanup_rig.graph
    assert graph.nodes[("Obligation", OBL_ABSORBED)] == _PROPERTIES
    assert ("MergedObligation", OBL_ABSORBED) not in graph.nodes
    assert _edges_of(cleanup_rig, OBL_ABSORBED) == [
        ("HAS", ROLE_ID, OBL_ABSORBED),
        ("REQUIRES", OBL_ABSORBED, "cap_1"),
        ("SATISFIED_BY", "req_1", OBL_ABSORBED),
        ("SATISFIED_BY", "req_2", OBL_ABSORBED),
    ]


def test_the_survivors_edges_are_never_touched(cleanup_rig: CleanupRig) -> None:
    obligation_unmerge_world(cleanup_rig)
    cleanup_rig.seed_endpoint("Requirement", "req_new")
    cleanup_rig.seed_edge(
        ("SATISFIED_BY", "Requirement", "req_new", "Obligation", OBL_SURVIVOR), legacy=False
    )
    before = _edges_of(cleanup_rig, OBL_SURVIVOR)

    outcome = run_obligation_unmerge(cleanup_rig)

    assert _edges_of(cleanup_rig, OBL_SURVIVOR) == before
    [group] = cleanup_rig.hooked.groups
    assert all(
        not isinstance(p, UpsertEdge) or OBL_SURVIVOR not in str(p) for p in group.primitives
    )
    assert outcome["unmerged"] is True


# -- the recreate is idempotent by id (AC-BI-004) -------------------------------------------------


def test_replaying_the_logged_group_recreates_the_same_graph(cleanup_rig: CleanupRig) -> None:
    from ps_service.graph_gateway.digest import canonical_digest

    obligation_unmerge_world(cleanup_rig)
    run_obligation_unmerge(cleanup_rig)
    after_once = canonical_digest(cleanup_rig.graph)
    [group] = cleanup_rig.hooked.groups

    # The same group once more (a replay of the log, or a retry of a half-applied append): the
    # Obligation already exists, so the upsert must not create a second node or edge.
    again = [p for p in group.primitives if not isinstance(p, DeleteNode)]
    cleanup_rig.gateway.submit_group(
        type(group)(graph=group.graph, primitives=tuple(again), audit_event_id=None)
    )

    assert canonical_digest(cleanup_rig.graph) == after_once
    assert len([k for k in cleanup_rig.graph.nodes if k == ("Obligation", OBL_ABSORBED)]) == 1


def test_an_unstorable_property_is_a_generic_persistence_error_with_no_content(
    cleanup_rig: CleanupRig,
) -> None:
    obligation_unmerge_world(cleanup_rig)
    plan = unmerge_plan(cleanup_rig, OBL_ABSORBED)
    assert isinstance(plan, ObligationUnmergePlan)
    unstorable = plan.model_copy(
        update={
            "write": plan.write.model_copy(update={"properties": {"identity": "secret-content"}})
        }
    )
    target = graph_write_target(cleanup_rig.dependencies(roles()), CONFIG)

    with pytest.raises(GraphCleanupPersistenceError) as raised:
        write_obligation_unmerge(cleanup_rig.read, target, audit_event_id="evt", plan=unstorable)

    assert "secret-content" not in str(raised.value)
    assert cleanup_rig.logged_groups == []


# -- the guard: the old WHERE, in Python (AC-BI-003) ----------------------------------------------


def _holds_after(rig: CleanupRig, break_it: Callable[[CleanupRig], None]) -> bool:
    """The planned write still holds on the graph as it is AFTER `break_it` changed it."""
    plan = unmerge_plan(rig, OBL_ABSORBED)
    assert isinstance(plan, ObligationUnmergePlan)
    break_it(rig)
    state = read_obligation_unmerge_state(rig.read, inputs=plan.inputs)
    return obligation_unmerge_guard_holds(
        state, plan.write, absorbed_id=OBL_ABSORBED, survivor_id=OBL_SURVIVOR
    )


def test_the_guard_holds_on_the_state_that_was_planned(cleanup_rig: CleanupRig) -> None:
    obligation_unmerge_world(cleanup_rig)

    assert _holds_after(cleanup_rig, lambda _rig: None) is True


def _no_marker(rig: CleanupRig) -> None:
    del rig.graph.nodes[("MergedObligation", OBL_ABSORBED)]


def _marker_repointed(rig: CleanupRig) -> None:
    rig.graph.nodes[("MergedObligation", OBL_ABSORBED)]["merged_into"] = "obl_other"


def _exists_again(rig: CleanupRig) -> None:
    rig.seed_endpoint("Obligation", OBL_ABSORBED, text="back")


def _role_gone(rig: CleanupRig) -> None:
    del rig.graph.nodes[("Role", ROLE_ID)]


def _requirement_gone(rig: CleanupRig) -> None:
    del rig.graph.nodes[("Requirement", "req_1")]


def _capability_gone(rig: CleanupRig) -> None:
    del rig.graph.nodes[("Capability", "cap_1")]


def _capability_tombstone(rig: CleanupRig) -> None:
    rig.graph.nodes[("Capability", "cap_1")]["status"] = "merged"


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
def test_the_guard_is_false_when_the_marker_the_obligation_the_role_or_an_endpoint_changed(
    cleanup_rig: CleanupRig, break_it: Callable[[CleanupRig], None]
) -> None:
    obligation_unmerge_world(cleanup_rig)

    assert _holds_after(cleanup_rig, break_it) is False


@pytest.mark.parametrize(
    "break_it", [_exists_again, _requirement_gone, _capability_tombstone, _role_gone]
)
def test_a_guard_miss_between_the_replan_and_the_write_logs_nothing(
    cleanup_rig: CleanupRig, break_it: Callable[[CleanupRig], None]
) -> None:
    obligation_unmerge_world(cleanup_rig)
    row = unmerge_row(cleanup_rig, OBL_ABSORBED)
    cleanup_rig.read.flip_before(_NODE_READ, 2, lambda: break_it(cleanup_rig))

    outcome = run_obligation_unmerge(cleanup_rig, row=row)

    assert "changed" in str(outcome["error"])
    assert _rows(cleanup_rig) == [
        ("obligation.unmerge", "applied"),
        ("obligation.unmerge", "failed"),
    ]
    assert cleanup_rig.audit.rows[1].details["reason_code"] == "graph_guard_missed"
    assert cleanup_rig.logged_groups == []
    assert ("MergedObligation", OBL_ABSORBED) in cleanup_rig.graph.nodes


def test_an_unrelated_write_between_the_position_read_and_the_append_is_refused(
    cleanup_rig: CleanupRig,
) -> None:
    obligation_unmerge_world(cleanup_rig)
    cleanup_rig.hooked.before_submit(cleanup_rig.append_unrelated_group)

    outcome = run_obligation_unmerge(cleanup_rig)

    assert "changed" in str(outcome["error"])
    assert cleanup_rig.audit.rows[1].details["reason_code"] == "graph_guard_missed"
    assert len(cleanup_rig.logged_groups) == 1  # only the unrelated group
    assert ("Obligation", OBL_ABSORBED) not in cleanup_rig.graph.nodes
    assert ("MergedObligation", OBL_ABSORBED) in cleanup_rig.graph.nodes


# -- legacy identity-less edges (AC-BI-005, F4) ---------------------------------------------------


def test_legacy_edges_are_stamped_first_so_no_parallel_edge_results(
    cleanup_rig: CleanupRig,
) -> None:
    obligation_unmerge_world(cleanup_rig, legacy=True)

    outcome = run_obligation_unmerge(cleanup_rig)

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
    obligation_unmerge_world(cleanup_rig, legacy=True)
    cleanup_rig.hooked.before_submit(cleanup_rig.append_unrelated_group)

    outcome = run_obligation_unmerge(cleanup_rig)

    assert "changed" in str(outcome["error"])
    event_id = cleanup_rig.audit.event_ids[0]
    links = [cleanup_rig.store.links[g.group_id] for g in cleanup_rig.logged_groups]
    assert links == [event_id, None]  # one stamp under the applied row; the rest is unrelated
    assert ("MergedObligation", OBL_ABSORBED) in cleanup_rig.graph.nodes


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
    obligation_unmerge_world(cleanup_rig)
    fail(cleanup_rig)

    outcome = run_obligation_unmerge(cleanup_rig)

    assert outcome == {"error": _WRITE_FAILED}
    assert _rows(cleanup_rig) == [
        ("obligation.unmerge", "applied"),
        ("obligation.unmerge", "failed"),
    ]
    assert cleanup_rig.audit.rows[1].details["reason_code"] == "graph_write_failed"
    assert "10.1.2.3" not in str(cleanup_rig.audit.rows[1].details)
    assert ("Obligation", OBL_ABSORBED) not in cleanup_rig.graph.nodes


def test_no_installed_gateway_is_a_failed_row_and_the_generic_write_failed_error(
    cleanup_rig: CleanupRig,
) -> None:
    obligation_unmerge_world(cleanup_rig)

    def _none(_config: ServiceConfig) -> object:
        raise GraphWriteUnavailableError("graph writes are unavailable: no gateway")

    dependencies = GraphCleanupDependencies(
        open_single_tenant_graph=lambda _config: cleanup_rig.read,
        audit_store=lambda _config: cleanup_rig.audit,
        access_role_store=lambda _config: roles(),
        graph_write_gateway=_none,  # pyright: ignore[reportArgumentType]
    )

    outcome = run_obligation_unmerge(cleanup_rig, dependencies=dependencies)

    assert outcome == {"error": _WRITE_FAILED}
    assert cleanup_rig.audit.rows[1].details["reason_code"] == "graph_write_failed"


def test_a_group_committed_but_not_applied_is_a_success_with_no_failed_row(
    cleanup_rig: CleanupRig,
) -> None:
    obligation_unmerge_world(cleanup_rig)
    cleanup_rig.graph.fail_on_write(
        redis.exceptions.ConnectionError("Error connecting to 10.1.2.3:6379")
    )

    outcome = run_obligation_unmerge(cleanup_rig)

    assert outcome["unmerged"] is True
    assert _rows(cleanup_rig) == [("obligation.unmerge", "applied")]
    assert len(cleanup_rig.hooked.groups) == 1
    assert "10.1.2.3" not in str(outcome)


def test_the_next_approval_while_the_graph_is_behind_its_log_writes_nothing(
    cleanup_rig: CleanupRig,
) -> None:
    obligation_unmerge_world(cleanup_rig)
    cleanup_rig.graph.fail_on_write(redis.exceptions.ConnectionError("down"))
    assert run_obligation_unmerge(cleanup_rig)["unmerged"] is True
    groups_before = len(cleanup_rig.logged_groups)

    second = run_obligation_unmerge(cleanup_rig)

    assert second == {"error": _WRITE_FAILED}
    assert len(cleanup_rig.logged_groups) == groups_before
