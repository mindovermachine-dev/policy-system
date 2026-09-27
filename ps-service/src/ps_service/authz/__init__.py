"""ps_service.authz -- package front door (issue #133).

Shared RBAC/ABAC authorization component used by both MCP Interface and (from
Slice 5) the REST API. Domain path: `ps.service.authz`
(`docs/architecture/ps-service-container-architecture.md`).

This slice (Slice 3) adds `SystemOwner` revoke, floor protection, and the
soft floor warning: `rules.enforce_system_owner_floor` (AC-BI-006) is now
reachable via `revoke_role`'s widened `SYSTEM_OWNER` support, alongside
Slice 1's `resolve_active_roles`/`require_role`/`list_assignments`, Slice
2's `grant_role`/`revoke_role` (`SystemAdmin`/`PolicyManager`) and
`rules.block_self_target` (AC-BI-005).

Re-exports the store/service front door and its domain-specific errors,
matching `ps_service.passkey_signing`'s own package front-door convention.
"""

from __future__ import annotations

from ps_service.authz.errors import (
    AccessRoleAssignmentPersistenceError,
    AccessRoleMigrationApplyError,
    AccessRolePostgresConnectionError,
    AccessRoleSystemOwnerFloorRaceError,
)
from ps_service.authz.models import AccessRole, AccessRoleAssignmentRow, AccessRoleGrantEvent
from ps_service.authz.rules import (
    AccessRule,
    AccessRuleContext,
    AccessRuleResult,
    block_self_target,
    enforce_system_owner_floor,
)
from ps_service.authz.service import (
    GrantResult,
    ListAssignmentsResult,
    RevokeResult,
    grant_role,
    list_assignments,
    require_role,
    resolve_active_roles,
    revoke_role,
)
from ps_service.authz.store import (
    AccessRoleStore,
    PsycopgAccessRoleStore,
    check_connectivity_from_config,
    connect_from_config,
)

__all__ = [
    "AccessRole",
    "AccessRoleAssignmentPersistenceError",
    "AccessRoleAssignmentRow",
    "AccessRoleGrantEvent",
    "AccessRoleMigrationApplyError",
    "AccessRolePostgresConnectionError",
    "AccessRoleStore",
    "AccessRoleSystemOwnerFloorRaceError",
    "AccessRule",
    "AccessRuleContext",
    "AccessRuleResult",
    "GrantResult",
    "ListAssignmentsResult",
    "PsycopgAccessRoleStore",
    "RevokeResult",
    "block_self_target",
    "check_connectivity_from_config",
    "connect_from_config",
    "enforce_system_owner_floor",
    "grant_role",
    "list_assignments",
    "require_role",
    "resolve_active_roles",
    "revoke_role",
]
