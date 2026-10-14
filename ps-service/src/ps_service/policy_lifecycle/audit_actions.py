"""Typed `details` models for the `policy.*` audit actions.

Registers each model with `ps_service.audit.models.register_audit_action` at import time, the same
"module import triggers registration" idiom `ps_service.authz.audit_actions` uses for its
`access_role.*` actions, and registers the `policy` audit resource type.

The component that emits `policy.*` audit events (`ps_service.policy_lifecycle.service`) imports
this module so registration happens before any event is recorded.
"""

from __future__ import annotations

from typing import Literal

from ps_service.audit.models import (
    AuditDetails,
    register_audit_action,
    register_audit_resource_type,
)


class PolicyCreateDraftDetails(AuditDetails):
    """Details of a `policy.create_draft` audit event (creation of a draft Policy or a fork).

    Attributes:
        affected_node_ids: Ids of the nodes the call created or would have created.
        to_status: Always `"draft"`.
        reason_code: Absent for `outcome='applied'`. For `outcome='rejected'` or `'failed'` it names
            the cause: a title collision, an unknown Capability, or a Capability that already has
            a governing Policy (including a lost claim race).
        supersedes_policy_id: `None` for an ordinary draft; the superseded Policy's id when the
            draft is a fork of an approved Policy.
        capability_ids: The Capabilities a fresh draft claims through `GOVERNED_BY` at creation. An
            `applied` event carries the intended ids; a `failed` event for a lost claim race
            carries `()` because nothing was claimed. An `applied` event followed by a `failed`
            event for the same resource therefore means the ids did not move. Always `()` for a
            fork.
    """

    affected_node_ids: tuple[str, ...]
    to_status: Literal["draft"] = "draft"
    reason_code: (
        Literal["title_already_exists", "capability_not_found", "capability_already_governed"]
        | None
    ) = None
    supersedes_policy_id: str | None = None
    capability_ids: tuple[str, ...] = ()


class PolicyTransitionDetails(AuditDetails):
    """Details of a `policy.propose`/`.approve`/`.reject`/`.revert`/`.auto_deprecate` audit event.

    One model covers all five transition actions, with a single open `reason_code` enum rather than
    one class per rejection reason.

    Attributes:
        affected_node_ids: Ids of the nodes the transition changed or would have changed.
        from_status: The Policy's status before the transition.
        to_status: The status the transition moves (or would move) the Policy to.
        reason_code: Absent for `outcome='applied'`. For `outcome='rejected'` or `'failed'` it names
            why the transition did not happen (access denied, self-approval blocked, invalid
            status, incomplete for proposal, governance conflict).
        capability_ids: On `policy.approve` of a fork, the Capabilities whose `GOVERNED_BY` edge
            moves from the superseded Policy to the approved one; `()` for every other transition.
            With `reason_code="governance_conflict"` (`outcome='failed'`) the governed set changed
            between read and write, so nothing moved and the event carries `()`; an `applied`
            event followed by a `failed` event for the same resource means the ids did not move.
    """

    affected_node_ids: tuple[str, ...]
    from_status: Literal["draft", "proposed", "approved"]
    to_status: Literal["draft", "proposed", "approved", "deprecated"]
    reason_code: (
        Literal[
            "access_denied",
            "self_approval_blocked",
            "invalid_status",
            "incomplete_for_proposal",
            "governance_conflict",
        ]
        | None
    ) = None
    capability_ids: tuple[str, ...] = ()


register_audit_action("policy.create_draft", PolicyCreateDraftDetails)
register_audit_action("policy.propose", PolicyTransitionDetails)
register_audit_action("policy.approve", PolicyTransitionDetails)
register_audit_action("policy.reject", PolicyTransitionDetails)
register_audit_action("policy.revert", PolicyTransitionDetails)
register_audit_action("policy.auto_deprecate", PolicyTransitionDetails)
register_audit_resource_type("policy")


__all__ = [
    "PolicyCreateDraftDetails",
    "PolicyTransitionDetails",
]
