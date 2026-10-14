"""Typed `details` models for the four `access_role.*` audit actions.

Registers each model with `ps_service.audit.models.register_audit_action` at import
time, and registers the `principal` audit resource type. Registration happens on import
of this module, the same way `@server.tool()` decorators populate the tool registry when
`ps_service.mcp_interface.mcp_server` is imported.

`ps_service.authz.store` imports this module for its side effect, so the actions are
registered before any grant, revoke or bootstrap call reaches `AuditStore.record`.

Side Effects:
    Importing this module registers `access_role.bootstrap`,
    `access_role.bootstrap_rejected`, `access_role.grant` and `access_role.revoke` as audit
    actions and `principal` as an audit resource type. Registration is process-global.

Raises:
    ValueError: at import time if one of the action names is already registered (raised
        by `register_audit_action`).
"""

from __future__ import annotations

from typing import Literal

from ps_service.audit.models import (
    AuditDetails,
    register_audit_action,
    register_audit_resource_type,
)


class AccessRoleBootstrapDetails(AuditDetails):
    """Details of an `access_role.bootstrap` audit event: the first owner was installed.

    Recorded only with `outcome='applied'`. A rejected bootstrap attempt is the separate
    action `access_role.bootstrap_rejected` (`AccessRoleBootstrapRejectedDetails`).

    Attributes:
        access_role: The role installed for the bootstrap principal; always `"SystemOwner"`.
    """

    access_role: Literal["SystemOwner"] = "SystemOwner"


class AccessRoleBootstrapRejectedDetails(AuditDetails):
    """Details of an `access_role.bootstrap_rejected` audit event.

    Recorded only with `outcome='rejected'`, when the assignment table is empty but the
    calling principal is not the operator-configured bootstrap owner.

    Attributes:
        reason_code: Why the attempt was rejected; always `"bootstrap_identity_mismatch"`.
    """

    reason_code: Literal["bootstrap_identity_mismatch"] = "bootstrap_identity_mismatch"


class AccessRoleGrantDetails(AuditDetails):
    """Details of an `access_role.grant` audit event.

    With `outcome='applied'` `reason_code` is `None`. With `outcome='rejected'` (the grant
    was denied) `reason_code` names the denial.

    Attributes:
        access_role: The role that was granted, or that the denied call attempted to grant.
        reason_code: `"access_denied"` (the actor lacks the required role),
            `"self_grant_blocked"` (the actor targeted themselves), or `None` when applied.
    """

    access_role: Literal["SystemOwner", "SystemAdmin", "PolicyManager", "ComplianceOfficer"]
    reason_code: Literal["access_denied", "self_grant_blocked"] | None = None


class AccessRoleRevokeDetails(AuditDetails):
    """Details of an `access_role.revoke` audit event.

    Same shape as `AccessRoleGrantDetails`, with the revoke-specific denial reasons. With
    `outcome='applied'` `reason_code` is `None`; with `outcome='rejected'` it names the
    denial.

    Attributes:
        access_role: The role that was revoked, or that the denied call attempted to revoke.
        reason_code: `"access_denied"` (the actor lacks the required role),
            `"self_revoke_blocked"` (the actor targeted themselves),
            `"system_owner_floor_violation"` (the revoke would leave zero active
            `SystemOwner`s), or `None` when applied.
    """

    access_role: Literal["SystemOwner", "SystemAdmin", "PolicyManager", "ComplianceOfficer"]
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
