"""Typed `details` models for the four `access_role.*` audit actions (issue #147).

Registers each model with `ps_service.audit.models.register_audit_action` at
import time -- the same "module import triggers registration" idiom
`ps_service.mcp_interface.mcp_server` already relies on for `@server.tool()`
decorators populating the live tool registry.

Nothing imports this module yet (PLAN.md/CHANGES.md Appendix A1): Slice 1
ships it inert -- `ps_service.authz.store`'s own side-effect import
(`import ps_service.authz.audit_actions  # noqa: F401`) lands in Slice 2,
when `store.py` is repointed to write through `AuditStore` in the same
transaction as the state change it is auditing.
"""

from __future__ import annotations

from typing import Literal

from ps_service.audit.models import (
    AuditDetails,
    register_audit_action,
    register_audit_resource_type,
)


class AccessRoleBootstrapDetails(AuditDetails):
    """`access_role.bootstrap` -- always `outcome='applied'`.

    The losing/rejected bootstrap path is a distinct action,
    `access_role.bootstrap_rejected` below, mirroring the historical split
    already in `ps_service.authz.store` between the winning bootstrap write
    and the `bootstrap_rejected` event type.
    """

    access_role: Literal["SystemOwner"] = "SystemOwner"


class AccessRoleBootstrapRejectedDetails(AuditDetails):
    """`access_role.bootstrap_rejected` -- always `outcome='rejected'` (AC-BI-012)."""

    reason_code: Literal["bootstrap_identity_mismatch"] = "bootstrap_identity_mismatch"


class AccessRoleGrantDetails(AuditDetails):
    """`access_role.grant`.

    `outcome='applied'`: `reason_code` is absent. `outcome='rejected'`
    (AC-BI-012's access-denied/self-grant-blocked denials): `reason_code` is
    populated. `access_role` names the role that was attempted, whether the
    grant succeeded or was denied.
    """

    access_role: Literal["SystemOwner", "SystemAdmin", "PolicyManager"]
    reason_code: Literal["access_denied", "self_grant_blocked"] | None = None


class AccessRoleRevokeDetails(AuditDetails):
    """`access_role.revoke` -- same shape as grant, plus the SystemOwner-floor reason code."""

    access_role: Literal["SystemOwner", "SystemAdmin", "PolicyManager"]
    reason_code: (
        Literal["access_denied", "self_revoke_blocked", "system_owner_floor_violation"] | None
    ) = None


register_audit_action("access_role.bootstrap", AccessRoleBootstrapDetails)
register_audit_action("access_role.bootstrap_rejected", AccessRoleBootstrapRejectedDetails)
register_audit_action("access_role.grant", AccessRoleGrantDetails)
register_audit_action("access_role.revoke", AccessRoleRevokeDetails)
register_audit_resource_type("principal")


__all__ = [
    "AccessRoleBootstrapDetails",
    "AccessRoleBootstrapRejectedDetails",
    "AccessRoleGrantDetails",
    "AccessRoleRevokeDetails",
]
