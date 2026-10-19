"""Generated values are fixed before submission and replay-identical.

A Capability's (and a draft Policy's) `created_at` is carried by the group as a literal, not
drawn by the writer at apply time: submitting the group twice leaves exactly one node with the
original value, and a replay of the log reproduces identical values (digest equal, `created_at`
equal to the logged text).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from company_merge._gateway_fakes import POLICY_SYSTEM, stepped_values
from ps_service.company_merge.graph_submission import GraphWriteTarget
from ps_service.company_merge.graph_writer import (
    build_canonical_node_primitives,
    build_draft_governance_primitives,
    persist_governance_drafts,
)
from ps_service.company_merge.models import BaselineNode, CanonicalResolution
from ps_service.company_merge.reviewable_kinds import CAPABILITY
from ps_service.graph_gateway.digest import canonical_digest
from ps_service.graph_gateway.models import MutationGroup

if TYPE_CHECKING:
    from company_merge._gateway_fakes import MergeRig


def _capability_group(rig: MergeRig) -> MutationGroup:
    node = BaselineNode("capability_one", {"name": "Encrypt data", "confidence": 0.8})
    resolution = CanonicalResolution(
        incoming_id=node.id, canonical_id=node.id, match_kind="new", embedding=(0.25, 0.5, 0.75)
    )
    primitives = build_canonical_node_primitives(
        set(), (node,), (resolution,), kind=CAPABILITY, values=stepped_values()
    )
    return MutationGroup(
        graph=POLICY_SYSTEM, audit_event_id=rig.audit_event_id, primitives=primitives
    )


def test_applying_the_same_capability_group_twice_leaves_one_node_with_the_original_created_at(
    merge_rig: MergeRig,
) -> None:
    group = _capability_group(merge_rig)

    first = merge_rig.gateway.submit_group(group)
    second = merge_rig.gateway.submit_group(group)

    assert (first.status, second.status) == ("applied", "unchanged")
    capabilities = [key for key in merge_rig.policy_system.nodes if key[0] == "Capability"]
    assert capabilities == [("Capability", "capability_one")]
    assert merge_rig.policy_system.nodes[("Capability", "capability_one")]["created_at"] == (
        "2026-03-01T12:00:00+00:00"
    )
    assert len(merge_rig.logged()) == 1


def test_a_replay_of_the_log_reproduces_the_exact_created_at_and_embedding(
    merge_rig: MergeRig,
) -> None:
    merge_rig.gateway.submit_group(_capability_group(merge_rig))
    before = canonical_digest(merge_rig.policy_system)
    logged = merge_rig.logged()[0]

    merge_rig.gateway.replay_graph(POLICY_SYSTEM)

    assert canonical_digest(merge_rig.policy_system) == before
    node = merge_rig.policy_system.nodes[("Capability", "capability_one")]
    assert node["created_at"] == logged.content["properties"]["created_at"]  # pyright: ignore[reportIndexIssue, reportArgumentType]
    assert node["embedding"] == [0.25, 0.5, 0.75]


def _draft_policy_group(rig: MergeRig) -> MutationGroup:
    policy = BaselineNode("pol_one", {"title": "Access Policy", "status": "approved"})
    primitives, _overridden = build_draft_governance_primitives(
        {},
        (policy,),
        (BaselineNode("std_one", {"title": "Access Standard"}),),
        (),
        owner=("sub-1", "https://idp.example"),
        policy_embeddings={"pol_one": (0.5, 0.25)},
        created_at=stepped_values().now().isoformat(),
    )
    return MutationGroup(
        graph=POLICY_SYSTEM, audit_event_id=rig.audit_event_id, primitives=primitives
    )


def test_applying_the_draft_policy_group_twice_leaves_one_policy_and_replay_reproduces_it(
    merge_rig: MergeRig,
) -> None:
    group = _draft_policy_group(merge_rig)

    first = merge_rig.gateway.submit_group(group)
    second = merge_rig.gateway.submit_group(group)

    assert (first.status, second.status) == ("applied", "unchanged")
    nodes = merge_rig.policy_system.nodes
    assert [key for key in nodes if key[0] == "Policy"] == [("Policy", "pol_one")]
    assert len(merge_rig.logged()) == 2  # the Policy and its Standard, logged once
    before = canonical_digest(merge_rig.policy_system)
    logged = merge_rig.logged()[0]

    merge_rig.gateway.replay_graph(POLICY_SYSTEM)

    assert canonical_digest(merge_rig.policy_system) == before
    policy = merge_rig.policy_system.nodes[("Policy", "pol_one")]
    assert policy["created_at"] == logged.content["properties"]["created_at"]  # pyright: ignore[reportIndexIssue, reportArgumentType]
    assert policy["created_at"] == "2026-03-01T12:00:00+00:00"
    assert policy["embedding"] == [0.5, 0.25]
    assert policy["status"] == "draft"


def test_the_draft_policy_created_at_is_drawn_once_before_the_group_is_submitted(
    merge_rig: MergeRig,
) -> None:
    """The persister draws one instant per call, ahead of every read and the submit."""
    values = stepped_values()
    persist_governance_drafts(
        GraphWriteTarget(merge_rig.gateway, POLICY_SYSTEM, merge_rig.audit_event_id),
        merge_rig.legacy,
        (BaselineNode("pol_a", {"title": "A"}), BaselineNode("pol_b", {"title": "B"})),
        (),
        (),
        owner=("sub-1", "https://idp.example"),
        policy_embeddings=None,
        values=values,
    )

    stamps = {
        merge_rig.policy_system.nodes[("Policy", e.identity)]["created_at"]
        for e in merge_rig.logged()
    }
    assert stamps == {"2026-03-01T12:00:00+00:00"}
    assert values.now().isoformat() == "2026-03-01T12:00:01+00:00"  # exactly one draw was taken
