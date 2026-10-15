"""Shared enforcement logic for access roles: role resolution, gating, grant and revoke.

The one implementation that both MCP tools and REST dependencies call in-process. Each
surface resolves its own caller identity first and then calls the same function here, so
there are never two parallel gating mechanisms.

Every function raises `AuthorizationStoreUnavailableError`, never silently returning an
empty or default result, whenever the underlying store raises
`StatePostgresConnectionError` or `AccessRoleAssignmentPersistenceError`. This is the single
place where the fail-closed contract is enforced, so no call site can forget it.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ps_service.api.errors import (
    AccessDeniedError,
    AuthorizationStoreUnavailableError,
    InvalidAccessRoleError,
    InvalidAuditQueryFilterError,
    SelfGrantOrRevokeBlockedError,
    SystemOwnerFloorViolationError,
)
from ps_service.audit.errors import (
    AuditInvalidCursorError,
    AuditPersistenceError,
    AuditPostgresUnavailableError,
)
from ps_service.audit.models import (
    AUDIT_DETAILS_FILTER_KEYS,
    is_known_resource_type,
    resolve_details_model,
)
from ps_service.authz.errors import (
    AccessRoleAssignmentPersistenceError,
    AccessRoleSystemOwnerFloorRaceError,
)
from ps_service.authz.models import AccessRole
from ps_service.authz.rules import AccessRuleContext, block_self_target, enforce_system_owner_floor
from ps_service.logging.errors import LoggingLifecycleError
from ps_service.logging.facade import emit_log_entry
from ps_service.persistence import StatePostgresConnectionError

if TYPE_CHECKING:
    from typing import Literal

    from ps_service.audit.models import AuditChainVerification, AuditQueryFilters, AuditQueryPage
    from ps_service.audit.store import AuditChainVerifier, AuditStore
    from ps_service.authz.models import AccessRoleAssignmentRow
    from ps_service.authz.store import AccessRoleStore

_ACCESS_DENIED_MESSAGE = "You do not have the required access role for this action."
_AUTHORIZATION_STORE_UNAVAILABLE_MESSAGE = "The authorization store is temporarily unavailable."
_INVALID_ACCESS_ROLE_MESSAGE = "The requested access role is not recognized."
_SELF_GRANT_OR_REVOKE_BLOCKED_MESSAGE = "You cannot grant or revoke your own access roles."
_SYSTEM_OWNER_FLOOR_VIOLATION_MESSAGE = "This action would leave zero active SystemOwners."
_SYSTEM_OWNER_FLOOR = 1

# `list_audit_events`'s filter-validation messages: each names the specific
# invalid filter and leaks no internal detail.
_INVALID_AUDIT_ACTION_FILTER_MESSAGE = "The 'action' filter names an action that is not registered."
_INVALID_AUDIT_RESOURCE_TYPE_FILTER_MESSAGE = (
    "The 'resource_type' filter names a resource type that is not registered."
)
_INVALID_AUDIT_TIME_RANGE_FILTER_MESSAGE = (
    "The 'occurred_from' filter must not be later than 'occurred_to'."
)
_INVALID_AUDIT_CURSOR_FILTER_MESSAGE = "The 'cursor' filter is malformed."
_INVALID_AUDIT_DETAILS_FILTER_MESSAGE = (
    "The 'details' filter accepts only the keys "
    f"{', '.join(AUDIT_DETAILS_FILTER_KEYS)}, each with a non-empty value."
)
# Upper bound on the page size of `list_audit_events`, so one request cannot
# trigger an unbounded read (`docs/coding-standards/level2-python-instructions.md`:
# constrain anything that flows into query construction).
_LIST_AUDIT_EVENTS_MAX_PAGE_SIZE = 100
_INVALID_AUDIT_PAGE_SIZE_FILTER_MESSAGE = (
    f"The 'page_size' filter must not exceed {_LIST_AUDIT_EVENTS_MAX_PAGE_SIZE}."
)

# Which `AccessRole`s `grant_role` accepts (the keys), and which roles the actor
# must hold to grant each (the values).
_GRANT_RBAC: dict[AccessRole, frozenset[AccessRole]] = {
    AccessRole.SYSTEM_ADMIN: frozenset({AccessRole.SYSTEM_OWNER}),
    AccessRole.POLICY_MANAGER: frozenset({AccessRole.SYSTEM_OWNER, AccessRole.SYSTEM_ADMIN}),
    AccessRole.SYSTEM_OWNER: frozenset({AccessRole.SYSTEM_OWNER}),
    AccessRole.COMPLIANCE_OFFICER: frozenset({AccessRole.SYSTEM_OWNER, AccessRole.SYSTEM_ADMIN}),
}

# Which `AccessRole`s `revoke_role` accepts (the keys), and which roles the actor
# must hold to revoke each (the values). Revoking `SYSTEM_ADMIN` requires
# `SystemOwner`; the other roles require `SystemOwner` or `SystemAdmin`. Letting a
# `SystemAdmin` revoke a `SystemOwner` makes the owner-floor rule reachable
# without the self-revoke block: once a second owner exists, an admin can remove
# one.
_REVOKE_RBAC: dict[AccessRole, frozenset[AccessRole]] = {
    AccessRole.SYSTEM_ADMIN: frozenset({AccessRole.SYSTEM_OWNER}),
    AccessRole.POLICY_MANAGER: frozenset({AccessRole.SYSTEM_OWNER, AccessRole.SYSTEM_ADMIN}),
    AccessRole.SYSTEM_OWNER: frozenset({AccessRole.SYSTEM_OWNER, AccessRole.SYSTEM_ADMIN}),
    AccessRole.COMPLIANCE_OFFICER: frozenset({AccessRole.SYSTEM_OWNER, AccessRole.SYSTEM_ADMIN}),
}

# A `SYSTEM_ADMIN` minimum is also satisfied by `SYSTEM_OWNER`, never the
# reverse, and never extended to `POLICY_MANAGER`. Kept as a small explicit
# allow-set rather than a generic role-ordering abstraction nothing else needs.
_SYSTEM_ADMIN_OR_ABOVE = frozenset({AccessRole.SYSTEM_ADMIN, AccessRole.SYSTEM_OWNER})


@dataclass(frozen=True, slots=True)
class ListAssignmentsResult:
    """What `list_assignments` returns: the full roster plus the `SystemOwner` floor warning.

    Attributes:
        assignments: Every persisted assignment row, ordered by principal subject, principal
            issuer and role; empty when there are none.
        system_owner_floor_warning: `True` when exactly one active `SystemOwner` exists.
    """

    assignments: tuple[AccessRoleAssignmentRow, ...]
    system_owner_floor_warning: bool


@dataclass(frozen=True, slots=True)
class GrantResult:
    """What `grant_role` returns: the `SystemOwner` floor warning.

    Attributes:
        system_owner_floor_warning: `True` when exactly one active `SystemOwner` exists after
            the grant, whichever role was granted.
    """

    system_owner_floor_warning: bool


@dataclass(frozen=True, slots=True)
class RevokeResult:
    """What `revoke_role` returns: the `SystemOwner` floor warning.

    Attributes:
        system_owner_floor_warning: `True` when exactly one active `SystemOwner` exists after
            the revoke.
    """

    system_owner_floor_warning: bool


def _maybe_log_system_owner_floor_warning(action: str, active_system_owner_count: int) -> None:
    """Log a warning when exactly one active `SystemOwner` remains.

    Emits one log entry with `component="authz"` and `outcome="warning"`. Called after
    `resolve_active_roles` (only when the call won the bootstrap), `list_assignments`,
    `grant_role` and `revoke_role`. The same condition is reported to the caller through
    `system_owner_floor_warning` in the result.

    A `LoggingLifecycleError` is swallowed, because these functions can run before logging is
    configured (for example in unit tests) and a missing log sink must never fail an otherwise
    successful call; the warning is a visibility aid, not part of the floor enforcement.

    Args:
        action: The calling function's name, recorded so a log reader can tell which call
            triggered the warning.
        active_system_owner_count: The count computed by the caller; nothing is logged unless
            it equals 1.
    """
    if active_system_owner_count != _SYSTEM_OWNER_FLOOR:
        return
    with contextlib.suppress(LoggingLifecycleError):
        emit_log_entry(
            component="authz",
            action=action,
            outcome="warning",
            extra={"active_system_owner_count": active_system_owner_count},
        )


def _parse_grantable_access_role(
    access_role: str, *, rbac: dict[AccessRole, frozenset[AccessRole]]
) -> AccessRole:
    """Resolve `access_role` to an `AccessRole` that this flow manages, or raise.

    Two failures collapse into the same `InvalidAccessRoleError`: the name is not an
    `AccessRole` at all, or it is a valid role this flow does not manage, for example
    `AuthenticatedUser`, which is never individually granted or revoked. This is the inner,
    surface-independent validation layer; an MCP tool's own `Literal[...]` parameter rejects
    other values earlier.

    Args:
        access_role: The caller-supplied role name, for example `"SystemAdmin"`.
        rbac: The grant or revoke table of the calling flow; its keys are the closed set of
            roles the flow accepts.

    Returns:
        The resolved `AccessRole`.

    Raises:
        InvalidAccessRoleError: `access_role` is not a role this flow manages.
    """
    try:
        role = AccessRole(access_role)
    except ValueError as exc:
        raise InvalidAccessRoleError(_INVALID_ACCESS_ROLE_MESSAGE) from exc
    if role not in rbac:
        raise InvalidAccessRoleError(_INVALID_ACCESS_ROLE_MESSAGE)
    return role


def _record_rejected_or_raise_unavailable(
    store: AccessRoleStore,
    *,
    action: Literal["grant", "revoke"],
    actor: tuple[str, str],
    target: tuple[str, str],
    access_role: AccessRole,
    reason_code: str,
) -> None:
    """Record one rejected audit event for a denied grant or revoke, or fail closed.

    Called immediately before `grant_role` or `revoke_role` raise a denial error (access
    denied, self-grant or self-revoke blocked, `SystemOwner` floor violation). If the audit
    write itself fails, the original denial is not returned: a denial that cannot be shown to
    have been durably logged is not a safe "denied" response, so
    `AuthorizationStoreUnavailableError` is raised instead.

    Args:
        store: The store whose `record_grant_rejected` or `record_revoke_rejected` performs
            the write.
        action: `"grant"` to call `record_grant_rejected`, `"revoke"` for
            `record_revoke_rejected`.
        actor: The denied caller's verified `(sub, iss)` identity.
        target: The `(sub, iss)` identity the denied call targeted.
        access_role: The role the denied call attempted to grant or revoke.
        reason_code: One of the `reason_code` values registered for the action in
            `ps_service.authz.audit_actions`.

    Raises:
        AuthorizationStoreUnavailableError: the audit write failed with
            `AuditPostgresUnavailableError` or `AuditPersistenceError`.

    Side Effects:
        Writes one `access_role.grant` or `access_role.revoke` audit event with
        `outcome='rejected'`.
    """
    record = store.record_grant_rejected if action == "grant" else store.record_revoke_rejected
    try:
        record(actor=actor, target=target, access_role=access_role, reason_code=reason_code)
    except (AuditPostgresUnavailableError, AuditPersistenceError) as exc:
        raise AuthorizationStoreUnavailableError(_AUTHORIZATION_STORE_UNAVAILABLE_MESSAGE) from exc


def resolve_active_roles(
    principal: tuple[str, str], *, store: AccessRoleStore
) -> frozenset[AccessRole]:
    """Resolve `principal`'s current active `AccessRole` set, bootstrapping the first owner.

    Looks up the principal's persisted roles. If any exist, returns them plus
    `AUTHENTICATED_USER`, which every authenticated caller holds. If the principal has no
    persisted roles, delegates to `store.bootstrap_first_owner`, which decides whether this
    call installs the first-ever `SystemOwner` (only for the configured bootstrap identity,
    only while the store is empty) or leaves the caller with `AUTHENTICATED_USER` alone.

    Args:
        principal: The caller's verified `(sub, iss)` identity.
        store: The store to resolve against.

    Returns:
        The principal's full active role set; always contains `AUTHENTICATED_USER`.

    Raises:
        AuthorizationStoreUnavailableError: the store raised `StatePostgresConnectionError`
            or `AccessRoleAssignmentPersistenceError`. The call never falls open.

    Side Effects:
        When the principal has no persisted roles and is the configured bootstrap owner of an
        empty store, inserts the `SYSTEM_OWNER` and `AUTHENTICATED_USER` rows, writes an
        `access_role.bootstrap` audit event, and logs the one-owner floor warning. When the
        store is empty but the principal is not the configured owner, an
        `access_role.bootstrap_rejected` audit event is written instead.
    """
    try:
        existing = store.active_roles_for(principal)
    except (StatePostgresConnectionError, AccessRoleAssignmentPersistenceError) as exc:
        raise AuthorizationStoreUnavailableError(_AUTHORIZATION_STORE_UNAVAILABLE_MESSAGE) from exc
    if existing:
        return existing | {AccessRole.AUTHENTICATED_USER}
    try:
        bootstrapped = store.bootstrap_first_owner(principal)
    except (StatePostgresConnectionError, AccessRoleAssignmentPersistenceError) as exc:
        raise AuthorizationStoreUnavailableError(_AUTHORIZATION_STORE_UNAVAILABLE_MESSAGE) from exc
    if AccessRole.SYSTEM_OWNER in bootstrapped:
        # This call won the once-ever bootstrap: exactly one SystemOwner exists,
        # so the warning condition holds without another store round trip.
        _maybe_log_system_owner_floor_warning("resolve_active_roles", _SYSTEM_OWNER_FLOOR)
    return bootstrapped


def require_role(
    principal: tuple[str, str], *, minimum: AccessRole, store: AccessRoleStore
) -> None:
    """Gate an action on `principal` holding at least the `minimum` access role.

    A `minimum` of `SYSTEM_ADMIN` is also satisfied by `SYSTEM_OWNER`; without that, the sole
    bootstrapped owner could never pass an admin-only gate. Any other `minimum` is satisfied
    only by that exact role.

    Args:
        principal: The caller's verified `(sub, iss)` identity.
        minimum: The least `AccessRole` that satisfies the gate.
        store: The store to resolve against.

    Returns:
        None. Returning normally means the gate passed.

    Raises:
        AccessDeniedError: the principal's active roles do not satisfy `minimum`.
        AuthorizationStoreUnavailableError: propagated from `resolve_active_roles` when the
            store is unavailable.

    Side Effects:
        As `resolve_active_roles`: may bootstrap the first owner when the principal has no
        persisted roles. No audit event is written for a denial.
    """
    active = resolve_active_roles(principal, store=store)
    satisfying = _SYSTEM_ADMIN_OR_ABOVE if minimum is AccessRole.SYSTEM_ADMIN else {minimum}
    if not (active & satisfying):
        raise AccessDeniedError(_ACCESS_DENIED_MESSAGE)


def list_assignments(
    principal: tuple[str, str], *, store: AccessRoleStore
) -> ListAssignmentsResult:
    """Return every access-role assignment plus the `SystemOwner` floor warning.

    The full roster is sensitive, so the call is gated on `SystemAdmin` or above first.

    Args:
        principal: The caller's verified `(sub, iss)` identity.
        store: The store to read from.

    Returns:
        A `ListAssignmentsResult` with the full roster and `system_owner_floor_warning`, true
        when exactly one active `SystemOwner` exists.

    Raises:
        AccessDeniedError: the principal does not hold `SystemAdmin` or above.
        AuthorizationStoreUnavailableError: the store raised `StatePostgresConnectionError`
            or `AccessRoleAssignmentPersistenceError`, during the role check or the reads.

    Side Effects:
        Those of `require_role` (possible first-owner bootstrap). Logs the one-owner floor
        warning when exactly one active `SystemOwner` exists.
    """
    require_role(principal, minimum=AccessRole.SYSTEM_ADMIN, store=store)
    try:
        assignments = store.list_all_assignments()
        active_system_owners = store.count_active_system_owners()
    except (StatePostgresConnectionError, AccessRoleAssignmentPersistenceError) as exc:
        raise AuthorizationStoreUnavailableError(_AUTHORIZATION_STORE_UNAVAILABLE_MESSAGE) from exc
    _maybe_log_system_owner_floor_warning("list_assignments", active_system_owners)
    return ListAssignmentsResult(
        assignments=assignments, system_owner_floor_warning=active_system_owners == 1
    )


def grant_role(
    *,
    actor: tuple[str, str],
    target_subject: str,
    access_role: str,
    store: AccessRoleStore,
    issuer: str,
) -> GrantResult:
    """Grant `access_role` to `target_subject`, subject to role and attribute checks.

    Checks run in order: (1) `access_role` names one of the four grantable roles; (2) the
    actor holds a role allowed to grant it: granting `SystemAdmin` or `SystemOwner` requires
    `SystemOwner`, granting `PolicyManager` or `ComplianceOfficer` requires `SystemOwner` or
    `SystemAdmin`; (3) `rules.block_self_target` allows it; (4) the store mutation.

    Args:
        actor: The granting caller's verified `(sub, iss)` identity.
        target_subject: The `sub` of the principal receiving the role.
        access_role: The role name: `"SystemOwner"`, `"SystemAdmin"`, `"PolicyManager"` or
            `"ComplianceOfficer"`.
        store: The store to mutate.
        issuer: This process's configured issuer, used as the target's `iss`.

    Returns:
        A `GrantResult` whose `system_owner_floor_warning` is true when exactly one active
        `SystemOwner` exists after the grant, whichever role was granted.

    Raises:
        InvalidAccessRoleError: `access_role` is not one of the four grantable roles.
        AccessDeniedError: the actor lacks a role allowed to grant it. A rejected
            `access_role.grant` audit event with `reason_code="access_denied"` is recorded
            first.
        SelfGrantOrRevokeBlockedError: the actor and the target are the same principal and
            no exemption applies. A rejected audit event with
            `reason_code="self_grant_blocked"` is recorded first.
        AuthorizationStoreUnavailableError: the store raised `StatePostgresConnectionError`
            or `AccessRoleAssignmentPersistenceError`, or the denial audit write itself
            failed.

    Side Effects:
        Success: inserts the assignment row (idempotent), writes an `access_role.grant`
        audit event with `outcome='applied'`, and logs the one-owner floor warning when it
        applies. Denial: writes a rejected audit event. Resolving the actor's roles may
        bootstrap the first owner (see `resolve_active_roles`).
    """
    role = _parse_grantable_access_role(access_role, rbac=_GRANT_RBAC)
    target = (target_subject, issuer)
    actor_roles = resolve_active_roles(actor, store=store)
    if not (actor_roles & _GRANT_RBAC[role]):
        _record_rejected_or_raise_unavailable(
            store,
            action="grant",
            actor=actor,
            target=target,
            access_role=role,
            reason_code="access_denied",
        )
        raise AccessDeniedError(_ACCESS_DENIED_MESSAGE)
    rule_result = block_self_target(
        AccessRuleContext(
            actor=actor,
            target=target,
            access_role=role,
            action="grant",
            active_system_owner_count=0,  # unused by block_self_target
            actor_is_system_owner=AccessRole.SYSTEM_OWNER in actor_roles,
        )
    )
    if not rule_result.allowed:
        _record_rejected_or_raise_unavailable(
            store,
            action="grant",
            actor=actor,
            target=target,
            access_role=role,
            reason_code="self_grant_blocked",
        )
        raise SelfGrantOrRevokeBlockedError(_SELF_GRANT_OR_REVOKE_BLOCKED_MESSAGE)
    try:
        store.grant(actor=actor, target=target, access_role=role)
        active_system_owners = store.count_active_system_owners()
    except (StatePostgresConnectionError, AccessRoleAssignmentPersistenceError) as exc:
        raise AuthorizationStoreUnavailableError(_AUTHORIZATION_STORE_UNAVAILABLE_MESSAGE) from exc
    _maybe_log_system_owner_floor_warning("grant_role", active_system_owners)
    return GrantResult(system_owner_floor_warning=active_system_owners == 1)


def revoke_role(
    *,
    actor: tuple[str, str],
    target_subject: str,
    access_role: str,
    store: AccessRoleStore,
    issuer: str,
) -> RevokeResult:
    """Revoke `access_role` from `target_subject`, subject to role and attribute checks.

    Checks run in order: (1) `access_role` names one of the four manageable roles; (2) the
    actor holds a role allowed to revoke it: revoking `SystemAdmin` requires `SystemOwner`,
    revoking any other role (including `SystemOwner`) requires `SystemOwner` or `SystemAdmin`;
    (3) `rules.block_self_target` allows it, evaluated before the floor check so the
    self-block wins when both apply; (4) for `SYSTEM_OWNER` only,
    `rules.enforce_system_owner_floor` against a count read just before; (5) the store
    mutation, which re-checks the floor atomically under an advisory lock to catch a
    concurrent revoke.

    Args:
        actor: The revoking caller's verified `(sub, iss)` identity.
        target_subject: The `sub` of the principal losing the role.
        access_role: The role name: `"SystemOwner"`, `"SystemAdmin"`, `"PolicyManager"` or
            `"ComplianceOfficer"`.
        store: The store to mutate.
        issuer: This process's configured issuer, used as the target's `iss`.

    Returns:
        A `RevokeResult` whose `system_owner_floor_warning` is true when exactly one active
        `SystemOwner` exists after the revoke.

    Raises:
        InvalidAccessRoleError: `access_role` is not one of the four manageable roles.
        AccessDeniedError: the actor lacks a role allowed to revoke it. A rejected
            `access_role.revoke` audit event with `reason_code="access_denied"` is recorded
            first.
        SelfGrantOrRevokeBlockedError: the actor and the target are the same principal and no
            exemption applies. A rejected audit event with
            `reason_code="self_revoke_blocked"` is recorded first.
        SystemOwnerFloorViolationError: revoking `SYSTEM_OWNER` would leave zero active
            `SystemOwner`s, found either by the pre-mutation rule or by the store's locked
            recount. A rejected audit event with
            `reason_code="system_owner_floor_violation"` is recorded first.
        AuthorizationStoreUnavailableError: the store raised `StatePostgresConnectionError`
            or `AccessRoleAssignmentPersistenceError`, or the denial audit write itself
            failed.

    Side Effects:
        Success: deletes the assignment row (absent is not an error), writes an
        `access_role.revoke` audit event with `outcome='applied'`, and logs the one-owner
        floor warning when it applies. Denial: writes a rejected audit event. Resolving the
        actor's roles may bootstrap the first owner (see `resolve_active_roles`).
    """
    role = _parse_grantable_access_role(access_role, rbac=_REVOKE_RBAC)
    target = (target_subject, issuer)
    actor_roles = resolve_active_roles(actor, store=store)
    if not (actor_roles & _REVOKE_RBAC[role]):
        _record_rejected_or_raise_unavailable(
            store,
            action="revoke",
            actor=actor,
            target=target,
            access_role=role,
            reason_code="access_denied",
        )
        raise AccessDeniedError(_ACCESS_DENIED_MESSAGE)
    rule_result = block_self_target(
        AccessRuleContext(
            actor=actor,
            target=target,
            access_role=role,
            action="revoke",
            active_system_owner_count=0,  # unused by block_self_target
            actor_is_system_owner=AccessRole.SYSTEM_OWNER in actor_roles,
        )
    )
    if not rule_result.allowed:
        _record_rejected_or_raise_unavailable(
            store,
            action="revoke",
            actor=actor,
            target=target,
            access_role=role,
            reason_code="self_revoke_blocked",
        )
        raise SelfGrantOrRevokeBlockedError(_SELF_GRANT_OR_REVOKE_BLOCKED_MESSAGE)
    if role is AccessRole.SYSTEM_OWNER:
        try:
            active_system_owner_count = store.count_active_system_owners()
        except (StatePostgresConnectionError, AccessRoleAssignmentPersistenceError) as exc:
            raise AuthorizationStoreUnavailableError(
                _AUTHORIZATION_STORE_UNAVAILABLE_MESSAGE
            ) from exc
        floor_result = enforce_system_owner_floor(
            AccessRuleContext(
                actor=actor,
                target=target,
                access_role=role,
                action="revoke",
                active_system_owner_count=active_system_owner_count,
            )
        )
        if not floor_result.allowed:
            _record_rejected_or_raise_unavailable(
                store,
                action="revoke",
                actor=actor,
                target=target,
                access_role=role,
                reason_code="system_owner_floor_violation",
            )
            raise SystemOwnerFloorViolationError(_SYSTEM_OWNER_FLOOR_VIOLATION_MESSAGE)
    try:
        store.revoke(actor=actor, target=target, access_role=role)
        active_system_owners = store.count_active_system_owners()
    except AccessRoleSystemOwnerFloorRaceError as exc:
        _record_rejected_or_raise_unavailable(
            store,
            action="revoke",
            actor=actor,
            target=target,
            access_role=role,
            reason_code="system_owner_floor_violation",
        )
        raise SystemOwnerFloorViolationError(_SYSTEM_OWNER_FLOOR_VIOLATION_MESSAGE) from exc
    except (StatePostgresConnectionError, AccessRoleAssignmentPersistenceError) as exc:
        raise AuthorizationStoreUnavailableError(_AUTHORIZATION_STORE_UNAVAILABLE_MESSAGE) from exc
    _maybe_log_system_owner_floor_warning("revoke_role", active_system_owners)
    return RevokeResult(system_owner_floor_warning=active_system_owners == 1)


def _validate_audit_query_filters(filters: AuditQueryFilters, *, page_size: int) -> None:
    """Validate the audit query filters, raising for the first invalid one found.

    Runs before the audit store is queried. Checks, in order: `action` is a registered audit
    action; `resource_type` is a registered resource type; `occurred_from` is not later than
    `occurred_to`; `page_size` does not exceed the maximum; every `details` key is in
    `AUDIT_DETAILS_FILTER_KEYS` and has a non-empty value. The `details` message lists the
    allowed keys but never echoes the caller's key. The cursor is not checked here, because
    only `AuditStore.query` knows its internal shape.

    Args:
        filters: The caller's audit query filters.
        page_size: The requested page size.

    Raises:
        InvalidAuditQueryFilterError: a filter or the page size is invalid; the message names
            which one.
    """
    if filters.action is not None and resolve_details_model(filters.action) is None:
        raise InvalidAuditQueryFilterError(_INVALID_AUDIT_ACTION_FILTER_MESSAGE)
    if filters.resource_type is not None and not is_known_resource_type(filters.resource_type):
        raise InvalidAuditQueryFilterError(_INVALID_AUDIT_RESOURCE_TYPE_FILTER_MESSAGE)
    if (
        filters.occurred_from is not None
        and filters.occurred_to is not None
        and filters.occurred_from > filters.occurred_to
    ):
        raise InvalidAuditQueryFilterError(_INVALID_AUDIT_TIME_RANGE_FILTER_MESSAGE)
    if page_size > _LIST_AUDIT_EVENTS_MAX_PAGE_SIZE:
        raise InvalidAuditQueryFilterError(_INVALID_AUDIT_PAGE_SIZE_FILTER_MESSAGE)
    if filters.details is not None and any(
        key not in AUDIT_DETAILS_FILTER_KEYS or not value for key, value in filters.details.items()
    ):
        raise InvalidAuditQueryFilterError(_INVALID_AUDIT_DETAILS_FILTER_MESSAGE)


def list_audit_events(
    principal: tuple[str, str],
    *,
    filters: AuditQueryFilters,
    cursor: str | None,
    page_size: int,
    access_role_store: AccessRoleStore,
    audit_store: AuditStore,
) -> AuditQueryPage:
    """Return one filtered, newest-first page of the audit trail.

    The caller must hold `SystemAdmin` or above; the role check runs before any filter
    validation or audit query. A malformed cursor is detected by the audit store and reported
    as an invalid filter.

    Args:
        principal: The caller's verified `(sub, iss)` identity.
        filters: The combinable audit filters (actor, resource, action, time range, details).
        cursor: The `next_cursor` of a previous page, or `None` for the first page.
        page_size: The number of events per page; must not exceed 100.
        access_role_store: The store used to resolve the caller's roles.
        audit_store: The store that is queried for audit events.

    Returns:
        An `AuditQueryPage` with the events, newest first, and the `next_cursor` for the
        following page (`None` when there is none).

    Raises:
        AccessDeniedError: the principal does not hold `SystemAdmin` or above.
        InvalidAuditQueryFilterError: a filter, the page size or the cursor is invalid; the
            message names which one.
        AuthorizationStoreUnavailableError: the role store raised
            `StatePostgresConnectionError` or `AccessRoleAssignmentPersistenceError`, or the
            audit store raised `AuditPostgresUnavailableError` or `AuditPersistenceError`.

    Side Effects:
        Those of `require_role` (possible first-owner bootstrap). The audit query itself is
        read-only.
    """
    require_role(principal, minimum=AccessRole.SYSTEM_ADMIN, store=access_role_store)
    _validate_audit_query_filters(filters, page_size=page_size)
    try:
        return audit_store.query(filters=filters, cursor=cursor, page_size=page_size)
    except AuditInvalidCursorError as exc:
        raise InvalidAuditQueryFilterError(_INVALID_AUDIT_CURSOR_FILTER_MESSAGE) from exc
    except (AuditPostgresUnavailableError, AuditPersistenceError) as exc:
        raise AuthorizationStoreUnavailableError(_AUTHORIZATION_STORE_UNAVAILABLE_MESSAGE) from exc


def verify_audit_log(
    principal: tuple[str, str],
    *,
    access_role_store: AccessRoleStore,
    audit_store: AuditChainVerifier,
) -> AuditChainVerification:
    """Recompute the audit hash chain and report whether it is intact.

    The caller must hold `SystemAdmin` or above; the role check runs before the audit store is
    touched, so a caller without the role triggers no verification at all.

    Args:
        principal: The caller's verified `(sub, iss)` identity.
        access_role_store: The store used to resolve the caller's roles.
        audit_store: The store whose chain is verified.

    Returns:
        An `AuditChainVerification`: intact, or the first (lowest-sequence) break.

    Raises:
        AccessDeniedError: the principal does not hold `SystemAdmin` or above.
        AuthorizationStoreUnavailableError: the role store raised
            `StatePostgresConnectionError` or `AccessRoleAssignmentPersistenceError`, or the
            audit store raised `AuditPostgresUnavailableError` or `AuditPersistenceError`.

    Side Effects:
        Those of `require_role` (possible first-owner bootstrap). The verification itself is
        read-only.
    """
    require_role(principal, minimum=AccessRole.SYSTEM_ADMIN, store=access_role_store)
    try:
        return audit_store.verify_integrity()
    except (AuditPostgresUnavailableError, AuditPersistenceError) as exc:
        raise AuthorizationStoreUnavailableError(_AUTHORIZATION_STORE_UNAVAILABLE_MESSAGE) from exc
