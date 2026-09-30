"""ps_service.authz core types (issue #133, PLAN.md §0.6/§1.1/§1.2).

`AccessRole` is deliberately named apart from `ps-domain-concepts.md`'s
`Role` node (a regulatory, RegulatoryInstrument-scoped compliance-spine
concept) -- this module's types are operational access-control data only:
never a FalkorDB graph node, never exposed via Cypher, never referenced from
`ps-domain-concepts.md` (PLAN.md §0.2).

`AccessRoleAssignmentRow`/`AccessRoleGrantEvent` mirror
`ps_service.passkey_signing.models`'s own "plain frozen dataclass, not
LLM/API-boundary Pydantic" convention -- neither shape crosses a REST/MCP
request/response boundary directly; they are `AccessRoleStore`'s own return
types.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from datetime import datetime
    from typing import Literal


class AccessRole(StrEnum):
    """The closed set of operational access-control roles (PLAN.md §0.6).

    `AccessRole("SomeOtherString")` raises `ValueError` -- the inner
    defensive layer for AC-BI-013 (the outer layer is each MCP tool's own
    `Literal[...]` schema parameter, which rejects any other value before
    the tool body ever runs).
    """

    AUTHENTICATED_USER = "AuthenticatedUser"
    SYSTEM_OWNER = "SystemOwner"
    SYSTEM_ADMIN = "SystemAdmin"
    POLICY_MANAGER = "PolicyManager"
    COMPLIANCE_OFFICER = "ComplianceOfficer"


@dataclass(frozen=True, slots=True)
class AccessRoleAssignmentRow:
    """One row of the `access_role_assignments` table (PLAN.md §1.1).

    `AUTHENTICATED_USER` is never persisted here for an ordinary grant (it
    is a base membership fact every already-authenticated caller has
    implicitly, PLAN.md §0.7) -- the one exception is the bootstrap event
    (AC-BI-001), which persists an explicit row for it alongside the
    `SYSTEM_OWNER` row so the first-ever principal's roster entry is
    genuinely auditable, not merely implied.
    """

    principal_subject: str
    principal_issuer: str
    access_role: AccessRole
    granted_at: datetime
    granted_by_subject: str
    granted_by_issuer: str


@dataclass(frozen=True, slots=True)
class AccessRoleGrantEvent:
    """One row of the permanent, insert-only grant/revoke audit event (an `audit_events` row)."""

    id: str
    event_type: Literal["bootstrap", "grant", "revoke", "bootstrap_rejected"]
    actor_subject: str
    actor_issuer: str
    target_subject: str
    target_issuer: str
    access_role: AccessRole
    occurred_at: datetime
