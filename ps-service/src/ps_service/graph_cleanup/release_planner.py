"""Pure planning and validation for `release-capability-governance` (issue #190, AC-BI-013/014).

No I/O: takes the `ReleaseState` the reader produced and returns the preview, the before/after
snapshots and a state digest that binds a passkey approval to the exact state previewed. Only a
`draft` governing Policy may lose a Capability (CHANGES.md D12 rejects `deprecated` like
`approved`); a non-draft Policy is rejected with an honest pointer to the policy lifecycle.
"""

from __future__ import annotations

import hashlib
import json

from ps_service.graph_cleanup.errors import GraphCleanupValidationError
from ps_service.graph_cleanup.models import (
    CapabilityNodeState,
    EdgeRecord,
    GoverningPolicy,
    GraphSnapshot,
    NodeRecord,
    ReleasePlan,
    ReleasePreview,
    ReleaseState,
)

__all__ = ["plan_release_governance", "validate_release_governance"]

_NOT_DRAFT_APPROVED = (
    "release-capability-governance works only on a draft policy. Policy {title!r} ({id}) is "
    "{status}, and a policy that is {status} changes only through the policy lifecycle (amend "
    "it by forking, see ps-policy-lifecycle). A fork carries the whole governed set, so it "
    "does not by itself free capability {capability!r}; no release is possible until that is "
    "addressed in the policy lifecycle."
)
_NOT_DRAFT_PROPOSED = (
    "release-capability-governance works only on a draft policy. Policy {title!r} ({id}) is "
    "proposed: its owner can return it to draft with revert-policy-to-draft, then release "
    "capability {capability!r} and propose the policy again."
)


def validate_release_governance(state: ReleaseState) -> tuple[CapabilityNodeState, GoverningPolicy]:
    """Reject a release that must never get an approval; return the (capability, policy).

    Raises:
        GraphCleanupValidationError: the capability does not exist, is a `merged` tombstone or
            not active, is not governed, has more than one governing policy (a data fault), or
            its governing policy is not a `draft` (AC-BI-014, D12).
    """
    capability = state.capability
    if capability is None:
        message = "the capability does not exist"
        raise GraphCleanupValidationError(message)
    if capability.status == "merged":
        message = f"capability {capability.id!r} is a merged tombstone and cannot be released"
        raise GraphCleanupValidationError(message)
    if capability.status != "active":
        message = f"capability {capability.id!r} is not active (status {capability.status!r})"
        raise GraphCleanupValidationError(message)
    if not state.policies:
        message = f"capability {capability.id!r} is not governed by any policy; nothing to release"
        raise GraphCleanupValidationError(message)
    policy, *others = state.policies
    if others:
        message = "a capability has more than one governing policy; this is a data fault"
        raise GraphCleanupValidationError(message)
    if policy.status != "draft":
        template = _NOT_DRAFT_PROPOSED if policy.status == "proposed" else _NOT_DRAFT_APPROVED
        raise GraphCleanupValidationError(
            template.format(
                title=policy.title, id=policy.id, status=policy.status, capability=capability.id
            )
        )
    return capability, policy


def _digest(before: GraphSnapshot) -> str:
    canonical = json.dumps(before.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def plan_release_governance(state: ReleaseState) -> ReleasePlan:
    """Plan releasing a Capability from its draft governing Policy (no I/O).

    Raises:
        GraphCleanupValidationError: see `validate_release_governance`.
    """
    capability, policy = validate_release_governance(state)
    nodes = (
        NodeRecord(
            label="Capability",
            id=capability.id,
            properties={
                **capability.properties,
                "name": capability.name,
                "status": capability.status,
            },
        ),
        NodeRecord(
            label="Policy",
            id=policy.id,
            properties={"title": policy.title, "status": policy.status},
        ),
    )
    edge = EdgeRecord(
        rel_type="GOVERNED_BY",
        source_label="Capability",
        source_id=capability.id,
        target_label="Policy",
        target_id=policy.id,
    )
    before = GraphSnapshot(nodes=nodes, edges=(edge,))
    after = GraphSnapshot(nodes=nodes, edges=())
    governed_before = tuple(sorted(set(state.governed_set) | {capability.id}))
    digest = _digest(before)
    preview = ReleasePreview(
        capability_id=capability.id,
        capability_name=capability.name,
        policy_id=policy.id,
        policy_title=policy.title,
        policy_status=policy.status,
        governed_set_before=governed_before,
        governed_set_after=tuple(sorted(set(governed_before) - {capability.id})),
        state_digest=digest,
    )
    return ReleasePlan(preview=preview, before=before, after=after, state_digest=digest)
