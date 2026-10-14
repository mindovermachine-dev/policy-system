"""The Policy Lifecycle side of the Graph Write Gateway seam.

`graph_submission.submit_group` is the one place a lifecycle writer's primitives reach the gateway:
an empty tuple submits nothing, the graph's legacy identity-less edges are stamped once per target
before its first group, and the group is submitted and required applied. These tests drive the real
seam over the real gateway; only the log store and the graph are the rig's in-memory boundaries.
"""

from __future__ import annotations

import pytest

from policy_lifecycle._gateway_fakes import POLICY_SYSTEM, LifecycleRig
from ps_service.domain_mapper.graph_writer import (
    _edge_identity as domain_mapper_edge_identity,  # pyright: ignore[reportPrivateUsage]
)
from ps_service.graph_gateway.errors import GraphWriteRejectedError
from ps_service.graph_gateway.label_allow_list import (
    ALLOWED_NODE_LABELS,
    ALLOWED_RELATIONSHIP_TYPES,
)
from ps_service.graph_gateway.models import ExpectedPosition, NodeRef, UpsertEdge, UpsertNode
from ps_service.policy_lifecycle.errors import PolicyLifecycleGraphUnavailableError, StaleGroupError
from ps_service.policy_lifecycle.graph_submission import (
    GraphWriteTarget,
    edge_identity,
    ensure_stamped,
    read_log_position,
    submit_group,
)

_EVENT_ID = "3f2b8c1e-5d4a-4b7e-9a61-0c2d7e8f9a10"


def _target(rig: LifecycleRig) -> GraphWriteTarget:
    return GraphWriteTarget(rig.gateway, POLICY_SYSTEM)


def _policy(policy_id: str) -> UpsertNode:
    return UpsertNode(label="Policy", id=policy_id, properties={"title": policy_id})


def test_an_empty_tuple_submits_nothing_and_stamps_nothing(lifecycle_rig: LifecycleRig) -> None:
    lifecycle_rig.seed_policy("P", standards=())
    lifecycle_rig.seed_policy("Q")
    lifecycle_rig.graph.seed_legacy_edge(("SUPERSEDED_BY", "Policy", "P", "Policy", "Q"), {})

    submit_group(_target(lifecycle_rig), (), audit_event_id=None)

    assert lifecycle_rig.logged_groups == 0
    assert ("SUPERSEDED_BY", "Policy", "P", "Policy", "Q", "") in lifecycle_rig.graph.edges


def test_a_group_with_no_audit_event_is_logged_with_a_null_link(
    lifecycle_rig: LifecycleRig,
) -> None:
    submit_group(_target(lifecycle_rig), (_policy("P"),), audit_event_id=None)

    (logged,) = lifecycle_rig.store.groups
    assert lifecycle_rig.store.audit_link_of(logged) is None
    assert ("Policy", "P") in lifecycle_rig.graph.nodes


def test_a_group_with_an_audit_event_is_anchored_to_it(lifecycle_rig: LifecycleRig) -> None:
    lifecycle_rig.store.audit_rows.add(_EVENT_ID)

    submit_group(_target(lifecycle_rig), (_policy("P"),), audit_event_id=_EVENT_ID)

    (logged,) = lifecycle_rig.store.read_groups_by_audit_event(_EVENT_ID)
    assert logged.graph == POLICY_SYSTEM


def test_legacy_edges_are_stamped_once_per_target_before_the_first_group(
    lifecycle_rig: LifecycleRig,
) -> None:
    lifecycle_rig.seed_policy("OLD")
    lifecycle_rig.seed_policy("NEW")
    lifecycle_rig.seed_supersedes("OLD", "NEW", legacy=True)
    target = _target(lifecycle_rig)

    submit_group(target, (_policy("A"),), audit_event_id=None)
    submit_group(target, (_policy("B"),), audit_event_id=None)

    edges = lifecycle_rig.graph.edges
    assert ("SUPERSEDED_BY", "Policy", "OLD", "Policy", "NEW", "SUPERSEDED_BY:OLD->NEW") in edges
    assert ("SUPERSEDED_BY", "Policy", "OLD", "Policy", "NEW", "") not in edges
    sizes = [len(group.entries) for group in lifecycle_rig.store.groups]
    assert sizes == [1, 1, 1]  # the stamp, then A, then B: the stamp is not repeated


def test_ensure_stamped_reports_how_many_edges_it_stamped_and_is_once_per_target(
    lifecycle_rig: LifecycleRig,
) -> None:
    lifecycle_rig.seed_policy("OLD")
    lifecycle_rig.seed_policy("NEW")
    lifecycle_rig.seed_supersedes("OLD", "NEW", legacy=True)
    target = _target(lifecycle_rig)

    first = ensure_stamped(target, audit_event_id=None)
    second = ensure_stamped(target, audit_event_id=None)

    assert (first, second) == (1, 0)
    assert lifecycle_rig.logged_groups == 1


def test_a_graph_with_nothing_to_stamp_logs_no_stamp_group(lifecycle_rig: LifecycleRig) -> None:
    assert ensure_stamped(_target(lifecycle_rig), audit_event_id=None) == 0

    assert lifecycle_rig.logged_groups == 0


def test_a_refused_group_is_the_persistence_error_with_the_write_failed_prefix(
    lifecycle_rig: LifecycleRig,
) -> None:
    bad = UpsertNode(label="NotInTheSchema", id="x")

    with pytest.raises(PolicyLifecycleGraphUnavailableError) as raised:
        submit_group(_target(lifecycle_rig), (bad,), audit_event_id=None)

    assert str(raised.value).startswith("FalkorDB write failed: ")
    assert isinstance(raised.value.__cause__, GraphWriteRejectedError)
    assert lifecycle_rig.logged_groups == 0


def test_a_stale_expected_position_is_a_stale_group_error_and_nothing_is_logged(
    lifecycle_rig: LifecycleRig,
) -> None:
    target = _target(lifecycle_rig)
    submit_group(target, (_policy("A"),), audit_event_id=None)
    logged = lifecycle_rig.logged_groups

    with pytest.raises(StaleGroupError) as raised:
        submit_group(
            target,
            (_policy("B"),),
            audit_event_id=None,
            preconditions=(ExpectedPosition(position=0),),
        )

    assert type(raised.value.__cause__).__name__ == "StaleGraphStateError"
    assert lifecycle_rig.logged_groups == logged
    assert ("Policy", "B") not in lifecycle_rig.graph.nodes


def test_a_matching_expected_position_submits_the_group(lifecycle_rig: LifecycleRig) -> None:
    target = _target(lifecycle_rig)
    submit_group(target, (_policy("A"),), audit_event_id=None)

    submit_group(
        target,
        (_policy("B"),),
        audit_event_id=None,
        preconditions=(ExpectedPosition(position=read_log_position(target)),),
    )

    assert ("Policy", "B") in lifecycle_rig.graph.nodes


def test_read_log_position_is_zero_on_a_fresh_graph_and_follows_the_log(
    lifecycle_rig: LifecycleRig,
) -> None:
    target = _target(lifecycle_rig)
    assert read_log_position(target) == 0

    submit_group(target, (_policy("A"), _policy("B")), audit_event_id=None)

    assert read_log_position(target) == 2


def test_the_error_without_a_message_keeps_its_fixed_text() -> None:
    assert (
        str(PolicyLifecycleGraphUnavailableError()) == "the policy graph database is not reachable"
    )


def test_the_error_with_a_message_carries_it() -> None:
    error = PolicyLifecycleGraphUnavailableError("FalkorDB write failed: x")

    assert str(error) == "FalkorDB write failed: x"


@pytest.mark.parametrize(
    ("edge_type", "source", "target"),
    [("GOVERNED_BY", "cap_a", "pol_1"), ("SUPPORTED_BY", "p", "s"), ("SUPERSEDED_BY", "a", "b")],
)
def test_the_edge_identity_agrees_with_the_domain_mapper_formula(
    edge_type: str, source: str, target: str
) -> None:
    assert edge_identity(edge_type, source, target) == domain_mapper_edge_identity(
        edge_type, source, target
    )
    assert edge_identity(edge_type, source, target) == f"{edge_type}:{source}->{target}"


def test_an_edge_with_the_lifecycle_identity_is_accepted_by_the_gateway(
    lifecycle_rig: LifecycleRig,
) -> None:
    target = _target(lifecycle_rig)
    edge = UpsertEdge(
        type="SUPPORTED_BY",
        identity=edge_identity("SUPPORTED_BY", "P", "S"),
        source=NodeRef(label="Policy", id="P"),
        target=NodeRef(label="Standard", id="S"),
    )

    submit_group(
        target,
        (_policy("P"), UpsertNode(label="Standard", id="S"), edge),
        audit_event_id=None,
    )

    assert ("SUPPORTED_BY", "Policy", "P", "Standard", "S", "SUPPORTED_BY:P->S") in (
        lifecycle_rig.graph.edges
    )


def test_the_gateway_allow_list_holds_every_label_and_type_the_lifecycle_writes() -> None:
    assert {"Policy", "Standard", "Control", "Capability"} <= ALLOWED_NODE_LABELS
    assert {"SUPPORTED_BY", "IMPLEMENTED_BY", "GOVERNED_BY", "SUPERSEDED_BY"} <= (
        ALLOWED_RELATIONSHIP_TYPES
    )
