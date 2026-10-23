"""Access-role and audit tools of the PS MCP server: roster management and the audit trail.

Registers `list-access-roles`, `grant-access-role`, `revoke-access-role`, `list-audit-events` and
`verify-audit-log` on the shared `server` from `tool_core`. This module imports only `tool_core`
(never another tool group); the patch seam names (`PsycopgAccessRoleStore`, `PsycopgAuditStore`)
are looked up there at call time as `tool_core.<Name>`.

Business Rules:
    - Every tool here is gated by `ACCESS_ROLE_MANAGEMENT_GATE` or `ACCESS_ROLE_CHANGE_GATE`
      (`SystemOwner` or `SystemAdmin`); a denied call writes one `access.gate` audit row.
    - The audit trail is read-only through these tools; integrity verification recomputes the hash
      chain and reports the first broken row without altering anything.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Annotated, Literal

from pydantic import Field

from ps_service.api.errors import (
    AccessDeniedError,
    AuthorizationStoreUnavailableError,
    InvalidAccessRoleError,
    InvalidAuditQueryFilterError,
    SelfGrantOrRevokeBlockedError,
    SystemOwnerFloorViolationError,
)
from ps_service.audit.models import AuditQueryFilters
from ps_service.authz.service import grant_role, list_assignments, revoke_role
from ps_service.authz.service import list_audit_events as run_list_audit_events
from ps_service.authz.service import verify_audit_log as run_verify_audit_log
from ps_service.config import load_config
from ps_service.mcp_interface import tool_core
from ps_service.mcp_interface.tool_core import ACCESS_ROLE_CHANGE_GATE as _ACCESS_ROLE_CHANGE_GATE
from ps_service.mcp_interface.tool_core import (
    ACCESS_ROLE_MANAGEMENT_GATE as _ACCESS_ROLE_MANAGEMENT_GATE,
)
from ps_service.mcp_interface.tool_core import gated_actor as _gated_actor
from ps_service.mcp_interface.tool_core import gated_server_tool as _gated_tool
from ps_service.mcp_interface.tool_core import resolve_authz_actor as _resolve_authz_actor
from ps_service.mcp_interface.tool_core import run_mcp_action as _run_mcp_action

if TYPE_CHECKING:
    from ps_service.audit.models import AuditChainVerification, AuditEventRow
    from ps_service.authz.models import AccessRoleAssignmentRow


# `from ... import X as _X` would need a 130-column line for this one name (E501), so it is bound
# by assignment instead.
_ACCESS_ROLE_MANAGEMENT_REQUIRES_AUTHENTICATED_CALLER_MESSAGE = (
    tool_core.ACCESS_ROLE_MANAGEMENT_REQUIRES_AUTHENTICATED_CALLER_MESSAGE
)


def _assignment_to_dict(row: AccessRoleAssignmentRow) -> dict[str, object]:
    """Shape one access-role assignment row for the `list-access-roles` response.

    Args:
        row: The assignment row read from the authorization store.

    Returns:
        A dict with `principal_subject`, `principal_issuer`, `access_role` (the role's string
        value),
        `granted_at` (ISO 8601 text), `granted_by_subject` and `granted_by_issuer`.

    Side Effects:
        None.
    """
    return {
        "principal_subject": row.principal_subject,
        "principal_issuer": row.principal_issuer,
        "access_role": row.access_role.value,
        "granted_at": row.granted_at.isoformat(),
        "granted_by_subject": row.granted_by_subject,
        "granted_by_issuer": row.granted_by_issuer,
    }


@_gated_tool("list-access-roles", _ACCESS_ROLE_MANAGEMENT_GATE)
def list_access_roles() -> dict[str, object] | str:
    """List every principal's current access-role assignments.

    Takes no parameters. The very first caller ever, of any identity, is bootstrapped to
    `AuthenticatedUser` plus `SystemOwner` before the access check runs, so that call always
    succeeds.
    Every later principal holds `AuthenticatedUser` alone by default, and this tool then requires
    `SystemAdmin` or `SystemOwner`. Requires a real authenticated session; the local-test bypass
    does
    not provide one.

    Returns:
        `{"assignments": [{"principal_subject", "principal_issuer", "access_role", "granted_at",
        "granted_by_subject", "granted_by_issuer"}, ...], "system_owner_floor_warning": bool}`.

    Errors (returned as a string beginning `error: `, never raised):
        - The caller has no real authenticated session.
        - The caller lacks the required role.
        - The authorization store cannot be reached.
        - Any other unexpected failure (a fixed generic message).

    Side Effects:
        Reads the authorization store. The first-ever call also writes the bootstrap assignments and
        their audit events. The call is logged.
        A caller refused by the role gate gets the unchanged `error: ` string and one
        best-effort `access.gate` audit row for this operation; no row is written for a caller
        with no verified identity.
    """
    config = load_config()
    actor = _gated_actor(config)

    def _body() -> dict[str, object] | str:
        store = tool_core.PsycopgAccessRoleStore(
            config, audit_store=tool_core.PsycopgAuditStore(config)
        )
        try:
            result = list_assignments(actor, store=store)
        except AuthorizationStoreUnavailableError as exc:
            return f"error: {exc}"
        return {
            "assignments": [_assignment_to_dict(row) for row in result.assignments],
            "system_owner_floor_warning": result.system_owner_floor_warning,
        }

    return _run_mcp_action("list_access_roles", _body)


_GRANT_REVOKE_ERRORS = (
    InvalidAccessRoleError,
    AccessDeniedError,
    SelfGrantOrRevokeBlockedError,
    SystemOwnerFloorViolationError,
    AuthorizationStoreUnavailableError,
)


@_gated_tool("grant-access-role", _ACCESS_ROLE_CHANGE_GATE)
def grant_access_role(
    principal_subject: Annotated[str, Field(min_length=1)],
    access_role: Literal["SystemOwner", "SystemAdmin", "PolicyManager", "ComplianceOfficer"],
) -> dict[str, object] | str:
    """Grant an access role to a principal.

    Granting `SystemAdmin` or `SystemOwner` requires the caller to hold `SystemOwner`. Granting
    `PolicyManager` or `ComplianceOfficer` requires `SystemOwner` or `SystemAdmin`. A caller may
    never
    grant a role to themselves. Requires a real authenticated session; the local-test bypass does
    not
    provide one.

    Args:
        principal_subject: Subject (`sub`) of the principal who receives the role; at least 1
            character.
        access_role: One of `SystemOwner`, `SystemAdmin`, `PolicyManager` or `ComplianceOfficer`;
            any
            other value is rejected before the tool runs.

    Returns:
        `{"principal_subject", "access_role", "granted_by_subject", "system_owner_floor_warning"}`.

    Errors (returned as a string beginning `error: `, never raised):
        - The caller has no real authenticated session.
        - `access_role` is not grantable.
        - The caller lacks the role the grant requires.
        - `principal_subject` is the caller.
        - The authorization store cannot be reached.
        - Any other unexpected failure (a fixed generic message).

    Side Effects:
        Persists the assignment in the authorization store and writes an audit event.
    """
    config = load_config()
    actor = _resolve_authz_actor(config)
    issuer = config.auth_issuer

    def _body() -> dict[str, object] | str:
        if actor is None or issuer is None:
            return _ACCESS_ROLE_MANAGEMENT_REQUIRES_AUTHENTICATED_CALLER_MESSAGE
        store = tool_core.PsycopgAccessRoleStore(
            config, audit_store=tool_core.PsycopgAuditStore(config)
        )
        try:
            result = grant_role(
                actor=actor,
                target_subject=principal_subject,
                access_role=access_role,
                store=store,
                issuer=issuer,
            )
        except _GRANT_REVOKE_ERRORS as exc:
            return f"error: {exc}"
        return {
            "principal_subject": principal_subject,
            "access_role": access_role,
            "granted_by_subject": actor[0],
            "system_owner_floor_warning": result.system_owner_floor_warning,
        }

    return _run_mcp_action("grant_access_role", _body)


@_gated_tool("revoke-access-role", _ACCESS_ROLE_CHANGE_GATE)
def revoke_access_role(
    principal_subject: Annotated[str, Field(min_length=1)],
    access_role: Literal["SystemOwner", "SystemAdmin", "PolicyManager", "ComplianceOfficer"],
) -> dict[str, object] | str:
    """Revoke an access role from a principal.

    Revoking `SystemOwner` requires the caller to hold `SystemOwner` or `SystemAdmin`. Revoking
    `SystemAdmin`, `PolicyManager` or `ComplianceOfficer` requires the same role that granting it
    requires. A caller may never revoke a role from themselves; that check runs before the floor
    check,
    which rejects revoking the last active `SystemOwner` whoever the caller is. Requires a real
    authenticated session; the local-test bypass does not provide one.

    Args:
        principal_subject: Subject (`sub`) of the principal who loses the role; at least 1
            character.
        access_role: One of `SystemOwner`, `SystemAdmin`, `PolicyManager` or `ComplianceOfficer`;
            any
            other value is rejected before the tool runs.

    Returns:
        `{"principal_subject", "access_role", "revoked_by_subject", "system_owner_floor_warning"}`.

    Errors (returned as a string beginning `error: `, never raised):
        - The caller has no real authenticated session.
        - `access_role` is not one of the four managed roles.
        - The caller lacks the role the revocation requires.
        - `principal_subject` is the caller.
        - The revocation would leave zero active `SystemOwner` principals.
        - The authorization store cannot be reached.
        - Any other unexpected failure (a fixed generic message).

    Side Effects:
        Updates the assignment in the authorization store and writes an audit event.
    """
    config = load_config()
    actor = _resolve_authz_actor(config)
    issuer = config.auth_issuer

    def _body() -> dict[str, object] | str:
        if actor is None or issuer is None:
            return _ACCESS_ROLE_MANAGEMENT_REQUIRES_AUTHENTICATED_CALLER_MESSAGE
        store = tool_core.PsycopgAccessRoleStore(
            config, audit_store=tool_core.PsycopgAuditStore(config)
        )
        try:
            result = revoke_role(
                actor=actor,
                target_subject=principal_subject,
                access_role=access_role,
                store=store,
                issuer=issuer,
            )
        except _GRANT_REVOKE_ERRORS as exc:
            return f"error: {exc}"
        return {
            "principal_subject": principal_subject,
            "access_role": access_role,
            "revoked_by_subject": actor[0],
            "system_owner_floor_warning": result.system_owner_floor_warning,
        }

    return _run_mcp_action("revoke_access_role", _body)


def _audit_event_to_dict(row: AuditEventRow) -> dict[str, object]:
    """Shape one audit event row for the `list-audit-events` response.

    Args:
        row: The audit event row read from the audit store.

    Returns:
        A dict with `id`, `occurred_at` (ISO 8601 text), `actor_subject`, `actor_issuer`, `action`,
        `resource_type`, `resource_id`, `outcome` and `details`.

    Side Effects:
        None.
    """
    return {
        "id": row.id,
        "occurred_at": row.occurred_at.isoformat(),
        "actor_subject": row.actor_subject,
        "actor_issuer": row.actor_issuer,
        "action": row.action,
        "resource_type": row.resource_type,
        "resource_id": row.resource_id,
        "outcome": row.outcome,
        "details": row.details,
    }


_LIST_AUDIT_EVENTS_ERRORS = (
    InvalidAuditQueryFilterError,
    AuthorizationStoreUnavailableError,
)


@_gated_tool("list-audit-events", _ACCESS_ROLE_MANAGEMENT_GATE)
def list_audit_events(  # noqa: PLR0913, PLR0917 -- every parameter is an independent, optional query filter; collapsing them into one payload object would just move the same count behind a wrapper
    actor_subject: Annotated[str, Field(min_length=1)] | None = None,
    actor_issuer: Annotated[str, Field(min_length=1)] | None = None,
    resource_type: Annotated[str, Field(min_length=1)] | None = None,
    resource_id: Annotated[str, Field(min_length=1)] | None = None,
    action: Annotated[str, Field(min_length=1)] | None = None,
    occurred_from: datetime | None = None,
    occurred_to: datetime | None = None,
    cursor: Annotated[str, Field(min_length=1)] | None = None,
    page_size: Annotated[int, Field(gt=0, le=100)] = 25,
    details: dict[str, str] | None = None,
) -> dict[str, object] | str:
    """Read the shared audit trail, filtered and paginated, newest first.

    Requires `SystemOwner` or `SystemAdmin`, because the trail is at least as sensitive as the role
    roster it partly documents, and a real authenticated session; the local-test bypass does not
    provide one. Every parameter is optional and filters combine.

    The trail includes the denials of role-gated operations (`access.gate`, resource type
    `operation`, the operation name as the resource id), passkey enrolments (`passkey.enroll`,
    resource type `pending_approval`) and `cypher` tool use (`query.cypher`, resource type
    `query`), next to the access-role, policy, ingestion, restore and other business rows. It does
    not hold authentication outcomes or rejected passkey steps.

    Args:
        actor_subject: Narrow to events by this actor subject; at least 1 character.
        actor_issuer: Narrow to events by this actor issuer; at least 1 character.
        resource_type: Narrow to this kind of acted-on resource; at least 1 character.
        resource_id: Narrow to this acted-on resource id; at least 1 character.
        action: Narrow to this namespaced action string, for example `access_role.grant`,
            `access.gate`, `passkey.enroll` or `query.cypher`; at least 1 character.
        occurred_from: Only events at or after this ISO 8601 time.
        occurred_to: Only events at or before this ISO 8601 time.
        cursor: The `next_cursor` of a previous call, to fetch the following page; at least 1
            character.
        page_size: Events per page, from 1 to 100; default 25.
        details: Exact-match filter on the business subject of a row. Allowed keys are `celex` (an
            ingestion run's submit and complete rows), `regulatory_instrument_id` (an ingestion's
            complete row) and `instrument_id` (restore rows), for example `{"celex": "32024R2847"}`.
            Any other key, or an empty value, is rejected.

    Returns:
        `{"events": [{"id", "occurred_at", "actor_subject", "actor_issuer", "action",
        "resource_type",
        "resource_id", "outcome", "details"}, ...], "next_cursor": str | None}`; `next_cursor` is
        `None` once no further events remain.

    Errors (returned as a string beginning `error: `, never raised):
        - The caller has no real authenticated session.
        - The caller lacks the required role.
        - A filter, `page_size` or `cursor` is invalid, including a disallowed `details` key.
        - The audit or authorization store cannot be reached.
        - Any other unexpected failure (a fixed generic message).

    Side Effects:
        Reads the audit trail; nothing is written. The call is logged.
        A caller refused by the role gate gets the unchanged `error: ` string and one
        best-effort `access.gate` audit row for this operation; no row is written for a caller
        with no verified identity.
    """
    config = load_config()
    actor = _gated_actor(config)

    def _body() -> dict[str, object] | str:
        access_role_store = tool_core.PsycopgAccessRoleStore(
            config, audit_store=tool_core.PsycopgAuditStore(config)
        )
        audit_store = tool_core.PsycopgAuditStore(config)
        filters = AuditQueryFilters(
            actor_subject=actor_subject,
            actor_issuer=actor_issuer,
            resource_type=resource_type,
            resource_id=resource_id,
            action=action,
            occurred_from=occurred_from,
            occurred_to=occurred_to,
            details=details,
        )
        try:
            page = run_list_audit_events(
                actor,
                filters=filters,
                cursor=cursor,
                page_size=page_size,
                access_role_store=access_role_store,
                audit_store=audit_store,
            )
        except _LIST_AUDIT_EVENTS_ERRORS as exc:
            return f"error: {exc}"
        return {
            "events": [_audit_event_to_dict(row) for row in page.events],
            "next_cursor": page.next_cursor,
        }

    return _run_mcp_action("list_audit_events", _body)


_VERIFY_AUDIT_LOG_ERRORS = (AuthorizationStoreUnavailableError,)


def _chain_break_to_dict(verification: AuditChainVerification) -> dict[str, object] | None:
    """The `first_break` object of `verify-audit-log`, or `None` for an intact chain.

    A row break names the row (`chain_seq`, `id`, `occurred_at`). A `head_mismatch` has no broken
    row, so it carries the head sequence instead.
    """
    first_break = verification.first_break
    if first_break is None:
        return None
    if first_break.kind == "head_mismatch":
        return {"kind": first_break.kind, "head_chain_seq": verification.head_chain_seq}
    return {
        "kind": first_break.kind,
        "chain_seq": first_break.chain_seq,
        "id": first_break.event_id,
        "occurred_at": (
            None if first_break.occurred_at is None else first_break.occurred_at.isoformat()
        ),
    }


@_gated_tool("verify-audit-log", _ACCESS_ROLE_MANAGEMENT_GATE)
def verify_audit_log() -> dict[str, object] | str:
    """Check that the shared audit trail has not been altered, deleted from or truncated.

    Requires `SystemOwner` or `SystemAdmin` and a real authenticated session; the local-test
    bypass does not provide one and is refused before any verification runs. Takes no
    arguments. The database recomputes every row's hash in order and compares the last row with
    the recorded chain head, in one consistent snapshot, so the check is safe to run while the
    service is writing.

    Returns:
        `{"intact": bool, "rows_checked": int, "head_chain_seq": int, "first_break": null | {...}}`.
        `intact` is `true` and `first_break` is `null` when the chain runs unbroken from its
        first row to the head. Otherwise `first_break` is the lowest-numbered failure: a row
        break is `{"kind", "chain_seq", "id", "occurred_at"}` with `kind` one of `sequence_gap`
        (a row is missing), `prev_hash_mismatch` or `row_hash_mismatch` (the row was altered);
        `{"kind": "head_mismatch", "head_chain_seq"}` means every remaining row verifies but
        the newest rows are gone (the tail was truncated).

    Errors (returned as a string beginning `error: `, never raised):
        - The caller has no real authenticated session.
        - The caller lacks the required role.
        - The audit or authorization store cannot be reached.
        - Any other unexpected failure (a fixed generic message).

    Side Effects:
        Reads the audit trail; nothing is written. The call is logged. The check is
        tamper-evident, not tamper-proof: a database superuser who rewrites the whole chain and
        its head consistently is not detected.
        A caller refused by the role gate gets the unchanged `error: ` string and one
        best-effort `access.gate` audit row for this operation; no row is written for a caller
        with no verified identity.
    """
    config = load_config()
    actor = _gated_actor(config)

    def _body() -> dict[str, object] | str:
        access_role_store = tool_core.PsycopgAccessRoleStore(
            config, audit_store=tool_core.PsycopgAuditStore(config)
        )
        audit_store = tool_core.PsycopgAuditStore(config)
        try:
            verification = run_verify_audit_log(
                actor, access_role_store=access_role_store, audit_store=audit_store
            )
        except _VERIFY_AUDIT_LOG_ERRORS as exc:
            return f"error: {exc}"
        return {
            "intact": verification.intact,
            "rows_checked": verification.rows_checked,
            "head_chain_seq": verification.head_chain_seq,
            "first_break": _chain_break_to_dict(verification),
        }

    return _run_mcp_action("verify_audit_log", _body)
