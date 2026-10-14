"""Propose, reject and revert cascade a Policy's tree as ONE gateway group.

The group is a `MergeProperty status` per node of the tree AS READ JUST BEFORE THE SUBMIT (no lock:
a Standard added between the gate read and the cascade read is cascaded too), linked to
the transition's `applied` audit row, written after that row, with no precondition (an unrelated
write between the read and the submit does not refuse a cascade). A gate rejection writes nothing;
a gateway failure records a `failed` row with the same details and the prefixed persistence error.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from authz._fakes import (  # pyright: ignore[reportPrivateUsage]  -- `tests/authz/` is an importable package; mirrors `test_service.py`'s cross-package import convention
    FakeAccessRoleStore,
)

from policy_lifecycle._gateway_fakes import (
    FlippingRead,
    PolicyReadGraph,
    SeedControl,
    SeedStandard,
)
from ps_service.authz.models import AccessRole
from ps_service.policy_lifecycle.errors import (
    PolicyDraftAccessDeniedError,
    PolicyLifecycleGraphUnavailableError,
)
from ps_service.policy_lifecycle.service import (
    propose_policy,
    reject_policy,
    revert_policy_to_draft,
)

if TYPE_CHECKING:
    from policy_lifecycle._gateway_fakes import LifecycleRig
    from ps_service.graph_gateway.models import GraphLogGroup

_OWNER = ("alice", "https://issuer.example")
_OTHER = ("bob", "https://issuer.example")
_MANAGER = ("carol", "https://issuer.example")
_GRANTER = ("system-admin-tool", "https://issuer.example")
_READ_TREE = "RETURN p.id, p.title, p.status, p.version"


def _tree(status: str) -> tuple[SeedStandard, ...]:
    return (
        SeedStandard(
            "std_1",
            "S1",
            status=status,
            controls=(
                SeedControl("ctl_1", "c1", status=status),
                SeedControl("ctl_2", "c2", status=status),
            ),
        ),
        SeedStandard("std_2", "S2", status=status, controls=(SeedControl("ctl_3", "c3", status),)),
    )


def _seed(
    rig: LifecycleRig, status: str, *, standards: tuple[SeedStandard, ...] | None = None
) -> None:
    rig.seed_policy(
        "pol_x",
        owner=_OWNER,
        status=status,
        standards=_tree(status) if standards is None else standards,
        legacy_edges=False,
    )


def _propose(rig: LifecycleRig, graph: PolicyReadGraph | None = None) -> None:
    propose_policy(
        actor=_OWNER,
        policy_id="pol_x",
        graph=rig.read if graph is None else graph,
        audit_store=rig.audit,
        submitter=rig.gateway,
    )


def _revert(rig: LifecycleRig, *, actor: tuple[str, str] = _OWNER) -> None:
    revert_policy_to_draft(
        actor=actor,
        policy_id="pol_x",
        graph=rig.read,
        audit_store=rig.audit,
        submitter=rig.gateway,
    )


def _merges(group: GraphLogGroup) -> dict[tuple[str, str], object]:
    """`{(label, id): status}` of every `merge_property` entry of `group`."""
    assert {str(entry.content["op"]) for entry in group.entries} == {"merge_property"}
    return {(entry.name, entry.identity): entry.content["properties"] for entry in group.entries}


def test_a_proposal_is_one_group_setting_the_status_of_every_node_of_the_tree(
    lifecycle_rig: LifecycleRig,
) -> None:
    _seed(lifecycle_rig, "draft")

    _propose(lifecycle_rig)

    (group,) = lifecycle_rig.store.groups
    assert _merges(group) == {
        ("Policy", "pol_x"): {"status": "proposed"},
        ("Standard", "std_1"): {"status": "proposed"},
        ("Control", "ctl_1"): {"status": "proposed"},
        ("Control", "ctl_2"): {"status": "proposed"},
        ("Standard", "std_2"): {"status": "proposed"},
        ("Control", "ctl_3"): {"status": "proposed"},
    }
    assert lifecycle_rig.store.audit_link_of(group) == lifecycle_rig.audit.event_ids[0]


def test_a_policy_with_no_children_cascades_its_own_status_alone(
    lifecycle_rig: LifecycleRig,
) -> None:
    _seed(lifecycle_rig, "proposed", standards=())

    _revert(lifecycle_rig)

    (group,) = lifecycle_rig.store.groups
    assert _merges(group) == {("Policy", "pol_x"): {"status": "draft"}}


def test_the_applied_row_precedes_the_append(lifecycle_rig: LifecycleRig) -> None:
    _seed(lifecycle_rig, "draft")

    _propose(lifecycle_rig)

    events = lifecycle_rig.events
    assert events.index("audit:policy.propose:applied") < next(
        i for i, event in enumerate(events) if "append" in event
    )


def test_a_standard_added_between_the_gate_read_and_the_cascade_read_is_cascaded_too(
    lifecycle_rig: LifecycleRig,
) -> None:
    """The tree is the one read just before the submit; no lock is taken."""
    _seed(lifecycle_rig, "draft")

    def _grow() -> None:
        lifecycle_rig.seed_standard_under("pol_x", SeedStandard("std_late", "Late"), legacy=False)

    _propose(lifecycle_rig, FlippingRead(lifecycle_rig.graph, _grow, marker=_READ_TREE))

    (group,) = lifecycle_rig.store.groups
    assert ("Standard", "std_late") in _merges(group)
    assert lifecycle_rig.graph.nodes[("Standard", "std_late")]["status"] == "proposed"
    # The audit row names the nodes of the gate read; the cascade covers more.
    assert "std_late" not in lifecycle_rig.audit.rows[0].details["affected_node_ids"]  # type: ignore[operator]


def test_a_gate_rejection_writes_a_rejected_row_and_nothing_to_the_graph(
    lifecycle_rig: LifecycleRig,
) -> None:
    _seed(lifecycle_rig, "draft")

    with pytest.raises(PolicyDraftAccessDeniedError):
        propose_policy(
            actor=_OTHER,
            policy_id="pol_x",
            graph=lifecycle_rig.read,
            audit_store=lifecycle_rig.audit,
            submitter=lifecycle_rig.gateway,
        )

    assert [row.outcome for row in lifecycle_rig.audit.rows] == ["rejected"]
    assert lifecycle_rig.store.groups == []


def test_a_gateway_failure_records_a_failed_row_with_the_same_details_and_the_prefixed_error(
    lifecycle_rig: LifecycleRig,
) -> None:
    _seed(lifecycle_rig, "draft")
    lifecycle_rig.store.fail_appends()

    with pytest.raises(PolicyLifecycleGraphUnavailableError) as raised:
        _propose(lifecycle_rig)

    assert str(raised.value).startswith("FalkorDB write failed: ")
    applied, failed = lifecycle_rig.audit.rows
    assert (applied.outcome, failed.outcome) == ("applied", "failed")
    assert failed.details == applied.details
    assert lifecycle_rig.graph.nodes[("Policy", "pol_x")]["status"] == "draft"


def test_an_unrelated_write_between_the_read_and_the_submit_does_not_refuse_a_cascade(
    lifecycle_rig: LifecycleRig,
) -> None:
    """The cascade has no precondition (unlike the claim and the fork repoint)."""
    _seed(lifecycle_rig, "draft")
    lifecycle_rig.hooked.before_submit(lifecycle_rig.append_unrelated_group)

    propose_policy(
        actor=_OWNER,
        policy_id="pol_x",
        graph=lifecycle_rig.read,
        audit_store=lifecycle_rig.audit,
        submitter=lifecycle_rig.hooked,
    )

    assert lifecycle_rig.graph.nodes[("Policy", "pol_x")]["status"] == "proposed"


def test_a_node_already_at_the_target_status_is_not_rewritten(lifecycle_rig: LifecycleRig) -> None:
    _seed(lifecycle_rig, "proposed")
    lifecycle_rig.graph.nodes[("Standard", "std_1")]["status"] = "draft"
    lifecycle_rig.graph.nodes[("Control", "ctl_1")]["status"] = "draft"
    lifecycle_rig.graph.nodes[("Control", "ctl_2")]["status"] = "draft"
    lifecycle_rig.graph.nodes[("Standard", "std_2")]["status"] = "draft"
    lifecycle_rig.graph.nodes[("Control", "ctl_3")]["status"] = "draft"

    _revert(lifecycle_rig)

    (group,) = lifecycle_rig.store.groups
    assert set(_merges(group)) == {("Policy", "pol_x")}


def test_a_rejection_cascades_the_tree_back_to_draft_under_its_own_applied_row(
    lifecycle_rig: LifecycleRig,
) -> None:
    _seed(lifecycle_rig, "proposed")
    roles = FakeAccessRoleStore()
    roles.grant(actor=_GRANTER, target=_MANAGER, access_role=AccessRole.POLICY_MANAGER)

    reject_policy(
        actor=_MANAGER,
        policy_id="pol_x",
        graph=lifecycle_rig.read,
        audit_store=lifecycle_rig.audit,
        access_role_store=roles,
        submitter=lifecycle_rig.gateway,
    )

    (group,) = lifecycle_rig.store.groups
    assert all(props == {"status": "draft"} for props in _merges(group).values())
    assert lifecycle_rig.audit.rows[0].action == "policy.reject"
    assert lifecycle_rig.store.audit_link_of(group) == lifecycle_rig.audit.event_ids[0]
