"""Role-based and attribute-based access control for the PS Service.

Shared authorization component used by both the MCP interface and the REST API. It owns
the operational access roles (`AccessRole`), their persisted assignments, the role-gating
checks (`require_role`), the grant/revoke operations with their attribute rules, and the
audit-event listing. Its domain path is `ps.service.authz`
(`docs/architecture/ps-service-container-architecture.md`).

Re-exports the service functions, the store seam and its PostgreSQL implementation, the
rule types, the component's domain-specific errors and `MIGRATIONS_DIR`, following the
package front-door convention of `ps_service.passkey_signing`.

Side Effects:
    Importing the package imports `ps_service.authz.store`, which imports
    `ps_service.authz.audit_actions` and thereby registers the `access_role.*` audit
    actions and the `principal` audit resource type with `ps_service.audit.models`.
"""

from __future__ import annotations

from pathlib import Path

from ps_service.authz.errors import (
    AccessRoleAssignmentPersistenceError,
    AccessRoleBootstrapConfigurationError,
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
    list_audit_events,
    require_role,
    resolve_active_roles,
    revoke_role,
)
from ps_service.authz.startup import require_bootstrap_owner_configured
from ps_service.authz.store import (
    AccessRoleStore,
    PsycopgAccessRoleStore,
)

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
"""This component's own migration directory, listed by the composition root (`ps_service.main`)."""

__all__ = [
    "MIGRATIONS_DIR",
    "AccessRole",
    "AccessRoleAssignmentPersistenceError",
    "AccessRoleAssignmentRow",
    "AccessRoleBootstrapConfigurationError",
    "AccessRoleGrantEvent",
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
    "enforce_system_owner_floor",
    "grant_role",
    "list_assignments",
    "list_audit_events",
    "require_bootstrap_owner_configured",
    "require_role",
    "resolve_active_roles",
    "revoke_role",
]
