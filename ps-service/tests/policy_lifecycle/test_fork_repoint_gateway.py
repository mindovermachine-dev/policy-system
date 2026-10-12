"""Approving a fork moves its prior's governed Capabilities as one guarded group (AC-BI-004).

The group deletes each `GOVERNED_BY` edge from the prior, upserts it to the fork and cascades the
fork's tree, guarded by the log position plus a Python re-read of the prior's governed set (CHANGES
M1 order: position, re-read, refuse with nothing logged, stamp, submit with `P0 + stamped`). A set
that changed, or any write that landed between the position read and the submit, is a governance
conflict: a `failed` row (`governance_conflict`, no Capabilities) and
`PolicyGovernanceConflictError`.
Approving also auto-deprecates an approved prior: a SECOND group under its own `applied` row.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from authz._fakes import (  # pyright: ignore[reportPrivateUsage]  -- `tests/authz/` is an importable package; mirrors `test_service.py`'s cross-package import convention
    FakeAccessRoleStore,
)

from policy_lifecycle._gateway_fakes import FlippingRead, PolicyReadGraph, SeedStandard
from ps_service.authz.models import AccessRole
from ps_service.policy_lifecycle.errors import (
    PolicyGovernanceConflictError,
    PolicyLifecycleGraphUnavailableError,
)
from ps_service.policy_lifecycle.service import PolicyApproveResult, approve_policy

if TYPE_CHECKING:
    from policy_lifecycle._gateway_fakes import LifecycleRig
    from ps_service.graph_gateway.models import GraphLogGroup
    from ps_service.policy_lifecycle.graph_submission import LifecycleSubmitter

_OWNER = ("alice", "https://issuer.example")
_MANAGER = ("carol", "https://issuer.example")
_GRANTER = ("system-admin-tool", "https://issuer.example")
_READ_GOVERNANCE = "RETURN prior.id, collect(cap.id) LIMIT 1"


def _seed_fork(
    rig: LifecycleRig, *, governed: tuple[str, ...] = ("c1", "c2"), legacy_edges: bool = True
) -> None:
    """A proposed fork `pol_new` of an approved prior `pol_old` that governs `governed`."""
    rig.seed_policy(
        "pol_old",
        owner=_OWNER,
        status="approved",
        standards=(SeedStandard("std_old", "Old", status="approved"),),
        legacy_edges=legacy_edges,
    )
    rig.seed_policy(
        "pol_new",
        owner=_OWNER,
        status="proposed",
        standards=(SeedStandard("std_new", "New", status="proposed"),),
        legacy_edges=legacy_edges,
    )
    rig.seed_supersedes("pol_old", "pol_new", legacy=legacy_edges)
    for capability_id in governed:
        rig.seed_capability(capability_id)
        rig.seed_governs(capability_id, "pol_old", legacy=legacy_edges)


def _approve(
    rig: LifecycleRig,
    policy_id: str = "pol_new",
    *,
    graph: PolicyReadGraph | None = None,
    submitter: LifecycleSubmitter | None = None,
) -> PolicyApproveResult:
    roles = FakeAccessRoleStore()
    roles.grant(actor=_GRANTER, target=_MANAGER, access_role=AccessRole.POLICY_MANAGER)
    return approve_policy(
        actor=_MANAGER,
        policy_id=policy_id,
        graph=rig.read if graph is None else graph,
        audit_store=rig.audit,
        access_role_store=roles,
        submitter=rig.gateway if submitter is None else submitter,
    )


def _ops(group: GraphLogGroup) -> list[str]:
    return [str(entry.content["op"]) for entry in group.entries]


def _governors(rig: LifecycleRig) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for edge_type, _, source, _, target, _ in rig.graph.edges:
        if edge_type == "GOVERNED_BY":
            result.setdefault(source, []).append(target)
    return result


def test_approving_a_non_fork_is_one_cascade_group_linked_to_the_applied_row(
    lifecycle_rig: LifecycleRig,
) -> None:
    lifecycle_rig.seed_policy(
        "pol_x",
        owner=_OWNER,
        status="proposed",
        standards=(SeedStandard("std_x", "X", status="proposed"),),
        legacy_edges=False,
    )

    result = _approve(lifecycle_rig, "pol_x")

    (group,) = lifecycle_rig.store.groups
    assert set(_ops(group)) == {"merge_property"}
    assert lifecycle_rig.store.audit_link_of(group) == lifecycle_rig.audit.event_ids[0]
    assert result.auto_deprecated_policy_id is None
    assert lifecycle_rig.graph.nodes[("Policy", "pol_x")]["status"] == "approved"
    assert lifecycle_rig.graph.nodes[("Standard", "std_x")]["status"] == "approved"


def test_approving_a_fork_moves_the_governed_capabilities_in_one_group(
    lifecycle_rig: LifecycleRig,
) -> None:
    """AC-BI-004: delete from the prior, upsert to the fork, cascade, one group."""
    _seed_fork(lifecycle_rig)

    result = _approve(lifecycle_rig)

    stamp, repoint, deprecation = lifecycle_rig.store.groups
    assert set(_ops(stamp)) == {"stamp_edge_identity"}  # legacy GOVERNED_BY edges are identity-less
    assert lifecycle_rig.store.audit_link_of(stamp) is None
    assert _ops(repoint) == [
        "delete_edge",
        "delete_edge",
        "upsert_edge",
        "upsert_edge",
        "merge_property",
        "merge_property",
    ]
    assert lifecycle_rig.store.audit_link_of(repoint) == lifecycle_rig.audit.event_ids[0]
    assert set(_ops(deprecation)) == {"merge_property"}
    assert _governors(lifecycle_rig) == {"c1": ["pol_new"], "c2": ["pol_new"]}
    assert result.governed_capability_ids == ("c1", "c2")
    assert result.auto_deprecated_policy_id == "pol_old"
    assert lifecycle_rig.graph.nodes[("Policy", "pol_new")]["status"] == "approved"


def test_the_applied_row_names_the_moved_capabilities_and_precedes_the_append(
    lifecycle_rig: LifecycleRig,
) -> None:
    _seed_fork(lifecycle_rig, legacy_edges=False)

    _approve(lifecycle_rig)

    approve_row, deprecate_row = lifecycle_rig.audit.rows
    assert (approve_row.action, approve_row.outcome) == ("policy.approve", "applied")
    assert approve_row.details["capability_ids"] == ["c1", "c2"]
    assert deprecate_row.action == "policy.auto_deprecate"
    events = lifecycle_rig.events
    assert events.index("audit:policy.approve:applied") < next(
        i for i, event in enumerate(events) if "append" in event
    )


def _assert_conflict_without_a_trace(rig: LifecycleRig, position: int) -> None:
    assert [row.outcome for row in rig.audit.rows] == ["applied", "failed"]
    failed = rig.audit.rows[1]
    assert failed.action == "policy.approve"
    assert failed.details["reason_code"] == "governance_conflict"
    assert failed.details["capability_ids"] == []
    assert rig.store.last_position("policy_system") == position  # no group, no stamp
    assert rig.graph.nodes[("Policy", "pol_new")]["status"] == "proposed"
    assert rig.graph.nodes[("Policy", "pol_old")]["status"] == "approved"


def test_a_capability_added_to_the_prior_after_the_read_is_a_conflict_and_logs_nothing(
    lifecycle_rig: LifecycleRig,
) -> None:
    """AC-BI-004."""
    _seed_fork(lifecycle_rig)
    lifecycle_rig.seed_capability("c3")
    position = lifecycle_rig.store.last_position("policy_system")
    graph = FlippingRead(
        lifecycle_rig.graph,
        lambda: lifecycle_rig.seed_governs("c3", "pol_old", legacy=True),
        marker=_READ_GOVERNANCE,
    )

    with pytest.raises(PolicyGovernanceConflictError):
        _approve(lifecycle_rig, graph=graph)

    _assert_conflict_without_a_trace(lifecycle_rig, position)


def test_a_capability_removed_from_the_prior_after_the_read_is_a_conflict_and_logs_nothing(
    lifecycle_rig: LifecycleRig,
) -> None:
    _seed_fork(lifecycle_rig)
    lifecycle_rig.seed_policy("pol_third", owner=_OWNER, legacy_edges=False)
    position = lifecycle_rig.store.last_position("policy_system")

    def _remove() -> None:
        lifecycle_rig.seed_governs("c2", "pol_third", legacy=True)
        for key in [k for k in lifecycle_rig.graph.edges if k[0] == "GOVERNED_BY" and k[2] == "c2"]:
            if key[4] == "pol_old":
                del lifecycle_rig.graph.edges[key]

    with pytest.raises(PolicyGovernanceConflictError):
        _approve(
            lifecycle_rig, graph=FlippingRead(lifecycle_rig.graph, _remove, marker=_READ_GOVERNANCE)
        )

    _assert_conflict_without_a_trace(lifecycle_rig, position)


def test_an_unrelated_write_between_the_position_read_and_the_submit_is_a_conflict(
    lifecycle_rig: LifecycleRig,
) -> None:
    """AC-BI-004, Decision 2: the position guard is coarser than the in-statement guard."""
    _seed_fork(lifecycle_rig)
    lifecycle_rig.hooked.before_submit(lifecycle_rig.append_unrelated_group)

    with pytest.raises(PolicyGovernanceConflictError):
        _approve(lifecycle_rig, submitter=lifecycle_rig.hooked)

    assert [row.outcome for row in lifecycle_rig.audit.rows] == ["applied", "failed"]
    stamp, injected = lifecycle_rig.store.groups  # the stamp stays, the refused group is absent
    assert set(_ops(stamp)) == {"stamp_edge_identity"}
    assert lifecycle_rig.store.audit_link_of(stamp) is None
    assert _ops(injected) == ["upsert_node"]
    assert _governors(lifecycle_rig) == {"c1": ["pol_old"], "c2": ["pol_old"]}
    assert lifecycle_rig.graph.nodes[("Policy", "pol_new")]["status"] == "proposed"


def test_a_fork_whose_prior_governs_nothing_approves_without_moving_edges(
    lifecycle_rig: LifecycleRig,
) -> None:
    """D-5: legacy forks without `GOVERNED_BY` edges stay approvable."""
    _seed_fork(lifecycle_rig, governed=(), legacy_edges=False)

    result = _approve(lifecycle_rig)

    assert result.governed_capability_ids == ()
    assert all("edge" not in op for group in lifecycle_rig.store.groups for op in _ops(group))
    assert lifecycle_rig.audit.rows[0].details["capability_ids"] == []


def test_the_auto_deprecation_is_a_second_group_under_its_own_applied_row(
    lifecycle_rig: LifecycleRig,
) -> None:
    """Preserved: two groups, not one."""
    _seed_fork(lifecycle_rig, legacy_edges=False)

    _approve(lifecycle_rig)

    approve_group, deprecate_group = lifecycle_rig.store.groups
    first, second = lifecycle_rig.audit.event_ids
    assert lifecycle_rig.store.audit_link_of(approve_group) == first
    assert lifecycle_rig.store.audit_link_of(deprecate_group) == second
    assert lifecycle_rig.audit.rows[1].action == "policy.auto_deprecate"
    assert lifecycle_rig.graph.nodes[("Policy", "pol_old")]["status"] == "deprecated"
    assert lifecycle_rig.graph.nodes[("Standard", "std_old")]["status"] == "deprecated"


def test_a_failure_of_the_second_group_leaves_the_first_applied(
    lifecycle_rig: LifecycleRig,
) -> None:
    """Preserved: the successor stays approved when the prior's deprecation cannot be written."""
    _seed_fork(lifecycle_rig, legacy_edges=False)
    lifecycle_rig.hooked.before_submit(
        lambda: lifecycle_rig.hooked.before_submit(lifecycle_rig.store.fail_appends)
    )

    with pytest.raises(PolicyLifecycleGraphUnavailableError) as raised:
        _approve(lifecycle_rig, submitter=lifecycle_rig.hooked)

    assert str(raised.value).startswith("FalkorDB write failed: ")
    assert [(row.action, row.outcome) for row in lifecycle_rig.audit.rows] == [
        ("policy.approve", "applied"),
        ("policy.auto_deprecate", "applied"),
        ("policy.auto_deprecate", "failed"),
    ]
    assert lifecycle_rig.graph.nodes[("Policy", "pol_new")]["status"] == "approved"
    assert lifecycle_rig.graph.nodes[("Policy", "pol_old")]["status"] == "approved"


def test_a_gateway_failure_of_the_repoint_records_failed_without_a_reason_code(
    lifecycle_rig: LifecycleRig,
) -> None:
    """AC-BI-011."""
    _seed_fork(lifecycle_rig, legacy_edges=False)
    lifecycle_rig.store.fail_appends()

    with pytest.raises(PolicyLifecycleGraphUnavailableError):
        _approve(lifecycle_rig)

    assert [row.outcome for row in lifecycle_rig.audit.rows] == ["applied", "failed"]
    assert "reason_code" not in lifecycle_rig.audit.rows[1].details
