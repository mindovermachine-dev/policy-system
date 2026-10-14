"""Core types of `ps_service.authz`: the access roles and the persisted assignment rows.

`AccessRole` is named apart from the `Role` node of `ps-domain-concepts.md` (a regulatory,
instrument-scoped compliance concept). The types here are operational access-control data
only: never a graph node, never exposed through Cypher, never referenced from the domain
concepts document.

`AccessRoleAssignmentRow` and `AccessRoleGrantEvent` are plain frozen dataclasses rather
than Pydantic models, because neither crosses a REST or MCP request/response boundary
directly; they are return types of `ps_service.authz.store.AccessRoleStore`.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from datetime import datetime
    from typing import Literal


class AccessRole(StrEnum):
    """The closed set of operational access-control roles.

    Members: `AUTHENTICATED_USER` (every authenticated caller implicitly holds it, never
    granted individually), `SYSTEM_OWNER`, `SYSTEM_ADMIN`, `POLICY_MANAGER` and
    `COMPLIANCE_OFFICER`. The enum value is the role's wire name, for example
    `"SystemOwner"`.

    Raises:
        ValueError: constructing `AccessRole(value)` with a string that is not a member's
            value. This is the inner defensive layer; each MCP tool's own `Literal[...]`
            parameter rejects other values before the tool body runs.
    """

    AUTHENTICATED_USER = "AuthenticatedUser"
    SYSTEM_OWNER = "SystemOwner"
    SYSTEM_ADMIN = "SystemAdmin"
    POLICY_MANAGER = "PolicyManager"
    COMPLIANCE_OFFICER = "ComplianceOfficer"


@dataclass(frozen=True, slots=True)
class AccessRoleAssignmentRow:
    """One row of the `access_role_assignments` table: a role held by a principal.

    `AUTHENTICATED_USER` is not persisted for an ordinary grant, because every authenticated
    caller holds it implicitly. The one exception is the bootstrap event, which persists an
    explicit `AUTHENTICATED_USER` row next to the `SYSTEM_OWNER` row so the first principal's
    roster entry is auditable rather than implied.

    Attributes:
        principal_subject: The holder's token `sub` claim.
        principal_issuer: The holder's token `iss` claim.
        access_role: The role held.
        granted_at: When the row was inserted (database time, timezone-aware).
        granted_by_subject: The `sub` of the granting actor, or `"system:bootstrap"` for
            rows created by the bootstrap event.
        granted_by_issuer: The `iss` of the granting actor, or `"system:bootstrap"` for
            rows created by the bootstrap event.
    """

    principal_subject: str
    principal_issuer: str
    access_role: AccessRole
    granted_at: datetime
    granted_by_subject: str
    granted_by_issuer: str


@dataclass(frozen=True, slots=True)
class AccessRoleGrantEvent:
    """One grant, revoke or bootstrap event of the permanent, insert-only audit trail.

    Attributes:
        id: The event's unique identifier.
        event_type: `"bootstrap"` (first owner installed), `"grant"`, `"revoke"`, or
            `"bootstrap_rejected"` (the empty store was claimed by a non-configured identity).
        actor_subject: The `sub` of the principal that performed the action.
        actor_issuer: The `iss` of the principal that performed the action.
        target_subject: The `sub` of the principal the action applied to.
        target_issuer: The `iss` of the principal the action applied to.
        access_role: The role granted, revoked or bootstrapped.
        occurred_at: When the event was recorded (timezone-aware).
    """

    id: str
    event_type: Literal["bootstrap", "grant", "revoke", "bootstrap_rejected"]
    actor_subject: str
    actor_issuer: str
    target_subject: str
    target_issuer: str
    access_role: AccessRole
    occurred_at: datetime
