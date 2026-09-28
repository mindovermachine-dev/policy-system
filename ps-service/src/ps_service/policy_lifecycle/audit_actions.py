"""Typed `details` models for the `policy.*` audit actions (issue #134).

Registers each model with `ps_service.audit.models.register_audit_action` at
import time -- the same "module import triggers registration" idiom
`ps_service.authz.audit_actions` already uses for its four `access_role.*`
actions.

Nothing imports this module yet (mirrors `ps_service.authz.audit_actions`'s
own note about its own Slice 1/Slice 2 split): the component that actually
emits `policy.*` audit events (a later slice's `ps_service.policy_lifecycle.
service`) is responsible for importing this module so registration happens
before any event is recorded.
"""

from __future__ import annotations

from typing import Literal

from ps_service.audit.models import (
    AuditDetails,
    register_audit_action,
    register_audit_resource_type,
)


class PolicyCreateDraftDetails(AuditDetails):
    """`policy.create_draft`.

    `outcome='applied'`: `reason_code` is absent. `outcome='rejected'`
    (AC-BI-022's title-collision rejection): `reason_code` is populated.
    """

    affected_node_ids: tuple[str, ...]
    to_status: Literal["draft"] = "draft"
    reason_code: Literal["title_already_exists"] | None = None


class PolicyTransitionDetails(AuditDetails):
    """`policy.propose`/`.approve`/`.reject`/`.revert`/`.auto_deprecate`.

    One shared model for all 5 transition actions -- mirroring how
    `ps_service.authz.audit_actions.AccessRoleRevokeDetails` already covers
    its action with a single, open `reason_code` enum rather than one class
    per rejection reason. `outcome='applied'`: `reason_code` is absent.
    `outcome='rejected'`: `reason_code` is populated with the specific
    reason the transition did not happen.
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
        ]
        | None
    ) = None


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
