"""Child ids derive from the parent and a re-add is idempotent.

A Standard's id is a function of its Policy and title, a Control's of its Standard and title, so
the same title under two parents never collides and adding the same child twice under one parent
is one node and one edge (the second call changes nothing, so it logs nothing). The gateway upserts
by id, so an id FORCED under a second parent (unreachable through the computed ids) does not mint a
second node.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from authz._fakes import (  # pyright: ignore[reportPrivateUsage]  -- `tests/authz/` is an importable package; mirrors `test_service.py`'s cross-package import convention
    FakeAccessRoleStore,
)

from policy_lifecycle._gateway_fakes import POLICY_SYSTEM, PolicyReadGraph, SeedStandard
from ps_service.domain_mapper.identity import control_id, standard_id
from ps_service.policy_lifecycle import graph_writer
from ps_service.policy_lifecycle.graph_submission import GraphWriteTarget
from ps_service.policy_lifecycle.service import (
    ControlDraftInput,
    StandardDraftInput,
    add_control_to_draft,
    add_standard_to_draft,
    create_policy_draft,
)

if TYPE_CHECKING:
    from policy_lifecycle._gateway_fakes import LifecycleRig

_OWNER = ("alice", "https://issuer.example")


def _seed(rig: LifecycleRig) -> None:
    for policy in ("pol_a", "pol_b"):
        rig.seed_policy(
            policy,
            owner=_OWNER,
            standards=(SeedStandard(f"std_{policy}", "S"),),
            legacy_edges=False,
        )


def _add_standard(rig: LifecycleRig, policy_id: str, title: str) -> str:
    return add_standard_to_draft(
        actor=_OWNER,
        policy_id=policy_id,
        title=title,
        fields={},
        graph=PolicyReadGraph(rig.graph),
        access_role_store=FakeAccessRoleStore(),
        submitter=rig.gateway,
        graph_name=POLICY_SYSTEM,
    ).standard_id


def _add_control(rig: LifecycleRig, parent_standard_id: str, title: str) -> str:
    return add_control_to_draft(
        actor=_OWNER,
        standard_id=parent_standard_id,
        title=title,
        control_type="manual",
        fields={},
        graph=PolicyReadGraph(rig.graph),
        access_role_store=FakeAccessRoleStore(),
        submitter=rig.gateway,
        graph_name=POLICY_SYSTEM,
    ).control_id


def _nodes(rig: LifecycleRig, label: str) -> list[str]:
    return [node_id for (node_label, node_id) in rig.graph.nodes if node_label == label]


def test_the_same_title_under_two_policies_mints_two_standards(lifecycle_rig: LifecycleRig) -> None:
    _seed(lifecycle_rig)

    first = _add_standard(lifecycle_rig, "pol_a", "Encryption")
    second = _add_standard(lifecycle_rig, "pol_b", "Encryption")

    assert first == standard_id("pol_a", "Encryption")
    assert second == standard_id("pol_b", "Encryption")
    assert first != second
    assert {first, second} <= set(_nodes(lifecycle_rig, "Standard"))


def test_the_same_title_under_two_standards_mints_two_controls(lifecycle_rig: LifecycleRig) -> None:
    _seed(lifecycle_rig)

    first = _add_control(lifecycle_rig, "std_pol_a", "Rotation")
    second = _add_control(lifecycle_rig, "std_pol_b", "Rotation")

    assert first == control_id("std_pol_a", "Rotation")
    assert first != second
    assert {first, second} <= set(_nodes(lifecycle_rig, "Control"))


def test_adding_the_same_standard_twice_leaves_one_node_and_one_edge_and_logs_once(
    lifecycle_rig: LifecycleRig,
) -> None:
    _seed(lifecycle_rig)
    first = _add_standard(lifecycle_rig, "pol_a", "Encryption")
    logged = lifecycle_rig.logged_groups

    second = _add_standard(lifecycle_rig, "pol_a", "Encryption")

    assert second == first
    assert _nodes(lifecycle_rig, "Standard").count(first) == 1
    parent_edges = [
        k for k in lifecycle_rig.graph.edges if k[0] == "SUPPORTED_BY" and k[4] == first
    ]
    assert len(parent_edges) == 1
    assert lifecycle_rig.logged_groups == logged


def test_adding_the_same_control_twice_leaves_one_node_and_one_edge(
    lifecycle_rig: LifecycleRig,
) -> None:
    _seed(lifecycle_rig)
    first = _add_control(lifecycle_rig, "std_pol_a", "Rotation")

    second = _add_control(lifecycle_rig, "std_pol_a", "Rotation")

    assert second == first
    assert _nodes(lifecycle_rig, "Control").count(first) == 1
    parent_edges = [
        k for k in lifecycle_rig.graph.edges if k[0] == "IMPLEMENTED_BY" and k[4] == first
    ]
    assert len(parent_edges) == 1


def test_an_id_forced_under_a_second_parent_does_not_mint_a_second_node(
    lifecycle_rig: LifecycleRig,
) -> None:
    """The one intentional divergence (issue "Discussion"): the gateway upserts by id."""
    _seed(lifecycle_rig)
    forced = standard_id("pol_a", "Encryption")
    for policy in ("pol_a", "pol_b"):
        graph_writer.add_standard_to_policy(
            GraphWriteTarget(lifecycle_rig.gateway, lifecycle_rig.single_tenant_graph_name),
            policy_id=policy,
            standard_id=forced,
            title="Encryption",
            extra_properties={},
        )

    assert _nodes(lifecycle_rig, "Standard").count(forced) == 1


def test_a_created_policys_children_are_derived_from_their_parents(
    lifecycle_rig: LifecycleRig,
) -> None:
    """The same Standard and Control titles in two created Policies get distinct ids."""
    for title in ("Policy One", "Policy Two"):
        create_policy_draft(
            actor=_OWNER,
            title=title,
            standards=(
                StandardDraftInput(title="Shared", controls=(ControlDraftInput(title="Shared"),)),
            ),
            graph=lifecycle_rig.read,
            audit_store=lifecycle_rig.audit,
            submitter=lifecycle_rig.gateway,
            graph_name=POLICY_SYSTEM,
        )

    assert len(_nodes(lifecycle_rig, "Standard")) == 2
    assert len(_nodes(lifecycle_rig, "Control")) == 2
