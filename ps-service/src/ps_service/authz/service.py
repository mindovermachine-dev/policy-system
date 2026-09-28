"""ps_service.authz shared enforcement logic (issue #133, PLAN.md §0.4/§2.3).

The one shared implementation both MCP tools and (from Slice 5) the REST
dependency call in-process -- mirrors `ps_service.passkey_signing.service`'s
own role exactly (PLAN.md §0.4 Pattern B): each surface resolves its own
caller identity first, then calls the exact same function here, never two
parallel gating mechanisms.

Every function below raises `AuthorizationStoreUnavailableError` (never
silently returns an empty/default result) whenever the underlying store
raises `AccessRolePostgresConnectionError`/`AccessRoleAssignmentPersistenceError`
-- the single place AC-BI-011's fail-closed contract is actually enforced,
so no call site can forget it.
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
from ps_service.audit.models import is_known_resource_type, resolve_details_model
from ps_service.authz.errors import (
    AccessRoleAssignmentPersistenceError,
    AccessRolePostgresConnectionError,
    AccessRoleSystemOwnerFloorRaceError,
)
from ps_service.authz.models import AccessRole
from ps_service.authz.rules import AccessRuleContext, block_self_target, enforce_system_owner_floor
from ps_service.logging.errors import LoggingLifecycleError
from ps_service.logging.facade import emit_log_entry

if TYPE_CHECKING:
    from typing import Literal

    from ps_service.audit.models import AuditQueryFilters, AuditQueryPage
    from ps_service.audit.store import AuditStore
    from ps_service.authz.models import AccessRoleAssignmentRow
    from ps_service.authz.store import AccessRoleStore

_ACCESS_DENIED_MESSAGE = "You do not have the required access role for this action."
_AUTHORIZATION_STORE_UNAVAILABLE_MESSAGE = "The authorization store is temporarily unavailable."
_INVALID_ACCESS_ROLE_MESSAGE = "The requested access role is not recognized."
_SELF_GRANT_OR_REVOKE_BLOCKED_MESSAGE = "You cannot grant or revoke your own access roles."
_SYSTEM_OWNER_FLOOR_VIOLATION_MESSAGE = "This action would leave zero active SystemOwners."
_SYSTEM_OWNER_FLOOR = 1

# issue #147, Slice 4: `list_audit_events`'s own filter-validation messages
# (AC-BI-008) -- each names the specific invalid filter, never leaking any
# internal detail (mirrors `InvalidAccessRoleError`'s own "leaks no internal
# detail" precedent).
_INVALID_AUDIT_ACTION_FILTER_MESSAGE = "The 'action' filter names an action that is not registered."
_INVALID_AUDIT_RESOURCE_TYPE_FILTER_MESSAGE = (
    "The 'resource_type' filter names a resource type that is not registered."
)
_INVALID_AUDIT_TIME_RANGE_FILTER_MESSAGE = (
    "The 'occurred_from' filter must not be later than 'occurred_to'."
)
_INVALID_AUDIT_CURSOR_FILTER_MESSAGE = "The 'cursor' filter is malformed."
# PLAN.md §4 Slice 4: a plan-original bound, not derived from any specific
# AC beyond AC-BI-008's "page size above the maximum" -- justified by L2's
# Data Modeling principle (`docs/coding-standards/level2-python-instructions.md`,
# "use Field() constraints on anything that flows into query construction")
# applied to this read path's own cost bound.
_LIST_AUDIT_EVENTS_MAX_PAGE_SIZE = 100
_INVALID_AUDIT_PAGE_SIZE_FILTER_MESSAGE = (
    f"The 'page_size' filter must not exceed {_LIST_AUDIT_EVENTS_MAX_PAGE_SIZE}."
)

# PLAN.md §0.7, widened per CHANGES.md Appendix A's MAJOR resolution (row 3,
# `SYSTEM_OWNER`, is the new one this repair adds): which `AccessRole`s
# `grant_role` accepts, and which roles the actor must hold to grant each.
_GRANT_RBAC: dict[AccessRole, frozenset[AccessRole]] = {
    AccessRole.SYSTEM_ADMIN: frozenset({AccessRole.SYSTEM_OWNER}),
    AccessRole.POLICY_MANAGER: frozenset({AccessRole.SYSTEM_OWNER, AccessRole.SYSTEM_ADMIN}),
    AccessRole.SYSTEM_OWNER: frozenset({AccessRole.SYSTEM_OWNER}),
}

# Revoke RBAC for `SYSTEM_ADMIN`/`POLICY_MANAGER` mirrors grant's own actor
# requirement -- PLAN.md's own text does not explicitly restate a separate
# revoke-RBAC rule for these two roles, so that part is a same-as-grant
# judgment call, not a directly-cited fact (Slice 2). `SYSTEM_OWNER`'s own
# revoke RBAC is CHANGES.md Appendix A's MAJOR resolution, widened from
# "actor must hold SystemOwner" to "actor must hold SystemOwner **or**
# SystemAdmin" -- this is what makes AC-BI-006/AC-BI-007 genuinely
# MCP-reachable (Slice 3): once a second SystemOwner exists, a SystemAdmin
# can revoke one without ever hitting the self-revoke block.
_REVOKE_RBAC: dict[AccessRole, frozenset[AccessRole]] = {
    AccessRole.SYSTEM_ADMIN: frozenset({AccessRole.SYSTEM_OWNER}),
    AccessRole.POLICY_MANAGER: frozenset({AccessRole.SYSTEM_OWNER, AccessRole.SYSTEM_ADMIN}),
    AccessRole.SYSTEM_OWNER: frozenset({AccessRole.SYSTEM_OWNER, AccessRole.SYSTEM_ADMIN}),
}

# PLAN.md §0.8's hierarchy fix: a `SYSTEM_ADMIN` minimum is also satisfied by
# `SYSTEM_OWNER` (never the reverse, and never extended to `POLICY_MANAGER`
# -- nothing in scope ever gates an action at `POLICY_MANAGER` that
# `SYSTEM_ADMIN` should also automatically satisfy). Implemented as a small,
# explicit allow-set for this one call site, not a generic transitive-order
# abstraction nothing else needs yet (YAGNI).
_SYSTEM_ADMIN_OR_ABOVE = frozenset({AccessRole.SYSTEM_ADMIN, AccessRole.SYSTEM_OWNER})


@dataclass(frozen=True, slots=True)
class ListAssignmentsResult:
    """What `list_assignments` returns: the full roster plus the `SystemOwner` floor warning."""

    assignments: tuple[AccessRoleAssignmentRow, ...]
    system_owner_floor_warning: bool


@dataclass(frozen=True, slots=True)
class GrantResult:
    """What `grant_role` returns: the `SystemOwner` floor warning (AC-BI-007)."""

    system_owner_floor_warning: bool


@dataclass(frozen=True, slots=True)
class RevokeResult:
    """What `revoke_role` returns: the `SystemOwner` floor warning (AC-BI-007)."""

    system_owner_floor_warning: bool


def _maybe_log_system_owner_floor_warning(action: str, active_system_owner_count: int) -> None:
    """AC-BI-007's "logged" half: warn whenever exactly one active SystemOwner remains.

    Mirrors `main.py`'s own `component="entrypoint"`/`action="startup"`/
    `outcome="warning"` convention (PLAN.md §4 Slice 3), scoped to this
    component as `component="authz"`. Called after every `resolve_active_roles`
    (bootstrap-win path only), `list_assignments`, `grant_role`, and
    `revoke_role` call that computes `active_system_owner_count` -- "visible
    to SystemOwners" is separately satisfied by `system_owner_floor_warning`
    on those same three tools' own response payloads (§0.9's list-gate means
    only a `SystemAdmin`-or-above caller ever sees either).

    Swallows `LoggingLifecycleError`: unlike `main.py`'s own call sites,
    which always run after `configure()` has already been called at process
    startup, `ps_service.authz.service`'s functions are also exercised
    directly by unit tests that never configure logging at all -- a missing
    log sink must never fail an otherwise-successful grant/revoke/bootstrap/
    list call (this warning is a visibility aid, not part of AC-BI-006's own
    enforcement).

    Args:
        action: The calling function's own name, recorded so a log reader
            can tell which call triggered the warning.
        active_system_owner_count: The count computed by the caller.
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
    """Resolve `access_role` to an `AccessRole` this flow manages, or raise (AC-BI-013).

    Two failure shapes collapse to the same `InvalidAccessRoleError` (PLAN.md
    §0.6's inner defensive layer): `access_role` doesn't construct as an
    `AccessRole` at all (`AccessRole(access_role)` raises `ValueError`), or it
    constructs fine but names a role this particular flow (`rbac`'s own key
    set) does not manage -- e.g. `AuthenticatedUser`, never individually
    grantable/revocable via these tools (PLAN.md §0.7).

    Args:
        access_role: The caller-supplied role name (already validated against
            the MCP tool's own `Literal[...]` schema at the outer layer, for
            an MCP-originated call -- this is the inner, surface-independent
            layer, also reachable by a direct call bypassing that schema).
        rbac: This call's own grant/revoke RBAC table -- also doubles as the
            closed set of roles this flow accepts.

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
    """Record one `outcome='rejected'` `audit_events` row for a grant/revoke denial (AC-BI-012).

    Called immediately before `grant_role`/`revoke_role` raise any of their
    four denial exceptions (access denied, self-grant/revoke blocked,
    SystemOwner floor violation) -- generalises the `bootstrap_rejected`
    mechanism `PsycopgAccessRoleStore.bootstrap_first_owner` already uses for
    the fourth denial type (bootstrap identity mismatch).

    Design decision (PLAN.md §4 Slice 3, CHANGES.md item 3): if the audit
    write itself fails (`AuditPostgresUnavailableError`/
    `AuditPersistenceError` -- e.g. the authz Postgres is unreachable), the
    *original* denial is never returned to the caller as-is. A denial that
    cannot be proven to have been durably logged is not a safe "denied"
    response (AC-BI-011's fail-closed contract applied in reverse): this
    raises `AuthorizationStoreUnavailableError` instead, which the caller's
    own `raise <DenialError>(...)` line never reaches.

    Args:
        store: The `AccessRoleStore` whose `record_grant_rejected`/
            `record_revoke_rejected` performs the actual write.
        action: Which of the two methods to call.
        actor: The denied caller's verified `(sub, iss)` identity.
        target: The principal the denied grant/revoke targeted.
        access_role: The role the denied call attempted to grant/revoke.
        reason_code: One of this action's registered `reason_code` values
            (`ps_service.authz.audit_actions`).

    Raises:
        AuthorizationStoreUnavailableError: the audit write failed -- the
            caller's own denial exception is never raised in this case.
    """
    record = store.record_grant_rejected if action == "grant" else store.record_revoke_rejected
    try:
        record(actor=actor, target=target, access_role=access_role, reason_code=reason_code)
    except (AuditPostgresUnavailableError, AuditPersistenceError) as exc:
        raise AuthorizationStoreUnavailableError(_AUTHORIZATION_STORE_UNAVAILABLE_MESSAGE) from exc


def resolve_active_roles(
    principal: tuple[str, str], *, store: AccessRoleStore
) -> frozenset[AccessRole]:
    """Resolve `principal`'s current active `AccessRole` set (AC-BI-001/002).

    Looks up `principal`'s own persisted rows first
    (`store.active_roles_for`) -- if any exist, returns them unioned with
    `AUTHENTICATED_USER` (every already-authenticated caller always has at
    least that). If `principal` has no persisted rows at all, delegates to
    `store.bootstrap_first_owner`, which itself decides whether this call
    wins the once-ever bootstrap race (AC-BI-001) or the store was already
    non-empty by another principal (AC-BI-002's default).

    Args:
        principal: The caller's verified `(sub, iss)` identity.
        store: The `AccessRoleStore` to resolve against.

    Returns:
        `principal`'s full active `AccessRole` set.

    Raises:
        AuthorizationStoreUnavailableError: never falls open on a store
            failure (AC-BI-011).
    """
    try:
        existing = store.active_roles_for(principal)
    except (AccessRolePostgresConnectionError, AccessRoleAssignmentPersistenceError) as exc:
        raise AuthorizationStoreUnavailableError(_AUTHORIZATION_STORE_UNAVAILABLE_MESSAGE) from exc
    if existing:
        return existing | {AccessRole.AUTHENTICATED_USER}
    try:
        bootstrapped = store.bootstrap_first_owner(principal)
    except (AccessRolePostgresConnectionError, AccessRoleAssignmentPersistenceError) as exc:
        raise AuthorizationStoreUnavailableError(_AUTHORIZATION_STORE_UNAVAILABLE_MESSAGE) from exc
    if AccessRole.SYSTEM_OWNER in bootstrapped:
        # This call won the once-ever bootstrap race -- the resulting active
        # SystemOwner count is deterministically 1 (AC-BI-001 creates exactly
        # one), so no extra store round trip is needed to know the warning
        # condition holds.
        _maybe_log_system_owner_floor_warning("resolve_active_roles", _SYSTEM_OWNER_FLOOR)
    return bootstrapped


def require_role(
    principal: tuple[str, str], *, minimum: AccessRole, store: AccessRoleStore
) -> None:
    """Raise `AccessDeniedError` unless `principal`'s active roles satisfy `minimum`.

    PLAN.md §0.8's hierarchy fix: a `minimum` of `SYSTEM_ADMIN` is also
    satisfied by `SYSTEM_OWNER` -- without this, the sole bootstrapped
    `SystemOwner` could never pass a literal `SystemAdmin`-only gate
    (AC-BI-001/005/008 would jointly describe a permanent lockout
    otherwise).

    Args:
        principal: The caller's verified `(sub, iss)` identity.
        minimum: The least `AccessRole` that satisfies this gate.
        store: The `AccessRoleStore` to resolve against.

    Raises:
        AccessDeniedError: `principal`'s active roles do not satisfy
            `minimum`.
        AuthorizationStoreUnavailableError: propagated from
            `resolve_active_roles` (AC-BI-011).
    """
    active = resolve_active_roles(principal, store=store)
    satisfying = _SYSTEM_ADMIN_OR_ABOVE if minimum is AccessRole.SYSTEM_ADMIN else {minimum}
    if not (active & satisfying):
        raise AccessDeniedError(_ACCESS_DENIED_MESSAGE)


def list_assignments(
    principal: tuple[str, str], *, store: AccessRoleStore
) -> ListAssignmentsResult:
    """Return every `access_role_assignments` row plus the `SystemOwner` floor warning.

    Gated at `require_role(principal, minimum=SYSTEM_ADMIN, store=store)`
    first (least-privilege -- the full roster is sensitive; this
    `SystemAdmin`-or-above gate is a plan-original design choice, PLAN.md
    §0.9, not derived from any specific AC).

    Args:
        principal: The caller's verified `(sub, iss)` identity.
        store: The `AccessRoleStore` to resolve against.

    Returns:
        The full roster plus whether exactly one active `SystemOwner`
        currently exists (AC-BI-007's warning condition).

    Raises:
        AccessDeniedError: `principal` does not hold `SystemAdmin` or above.
        AuthorizationStoreUnavailableError: propagated from any underlying
            store failure (AC-BI-011).
    """
    require_role(principal, minimum=AccessRole.SYSTEM_ADMIN, store=store)
    try:
        assignments = store.list_all_assignments()
        active_system_owners = store.count_active_system_owners()
    except (AccessRolePostgresConnectionError, AccessRoleAssignmentPersistenceError) as exc:
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
    """Grant `access_role` to `target_subject` (AC-BI-003/004/005/013).

    Order of checks (PLAN.md §2.3): (1) `access_role` resolves to one of the
    three grantable roles (else `InvalidAccessRoleError`); (2) `actor`'s own
    RBAC per the widened 3-row table (CHANGES.md Appendix A) -- granting
    `SystemAdmin` or `SystemOwner` requires `actor` hold `SystemOwner`;
    granting `PolicyManager` requires `SystemOwner` or `SystemAdmin` (else
    `AccessDeniedError`); (3) `rules.block_self_target` (else
    `SelfGrantOrRevokeBlockedError`); (4) the store mutation.

    Args:
        actor: The granting caller's verified `(sub, iss)` identity.
        target_subject: The principal to grant the role to (`iss` is
            implied -- PLAN.md §0.12, always this process's own configured
            issuer).
        access_role: The caller-supplied role name -- `"SystemOwner"`,
            `"SystemAdmin"`, or `"PolicyManager"`.
        store: The `AccessRoleStore` to mutate.
        issuer: This process's configured issuer, filled in as the target's
            `principal_issuer` (PLAN.md §0.12).

    Returns:
        `GrantResult(system_owner_floor_warning=...)` -- true when exactly
        one active `SystemOwner` exists after this grant (AC-BI-007),
        regardless of which role was just granted.

    Raises:
        InvalidAccessRoleError: `access_role` is not one of the three
            grantable roles (AC-BI-013).
        AccessDeniedError: `actor` does not hold the role this grant
            requires -- one `outcome='rejected'` `access_role.grant` audit
            event (`reason_code="access_denied"`) is recorded first
            (AC-BI-012).
        SelfGrantOrRevokeBlockedError: `actor` and the target are the same
            principal (AC-BI-005) -- likewise recorded first
            (`reason_code="self_grant_blocked"`, AC-BI-012).
        AuthorizationStoreUnavailableError: propagated from any underlying
            store failure (AC-BI-011), including a failed denial-audit
            write itself (`_record_rejected_or_raise_unavailable`).
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
    except (AccessRolePostgresConnectionError, AccessRoleAssignmentPersistenceError) as exc:
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
    """Revoke `access_role` from `target_subject` (AC-BI-005/006/013).

    `access_role` may now resolve to `SYSTEM_OWNER` too (Slice 3,
    CHANGES.md Appendix A) -- revoke RBAC for it is widened to "actor holds
    `SystemOwner` **or** `SystemAdmin`" (§_REVOKE_RBAC). Check order: (1)
    closed-set/RBAC-table membership, (2) actor's own RBAC, (3)
    `rules.block_self_target` (always -- evaluated *before* the floor
    check, so self-block wins whenever both conditions would otherwise
    apply, e.g. the sole `SystemOwner` revoking their own role), (4)
    `rules.enforce_system_owner_floor` (only when `access_role ==
    SYSTEM_OWNER`, against a count read immediately before this check), (5)
    the store mutation, which re-checks the floor a second time, atomically,
    under an advisory lock (`AccessRoleSystemOwnerFloorRaceError`, PLAN.md
    §0.10 -- defense against a genuine concurrent-revoke race).

    Args:
        actor: The revoking caller's verified `(sub, iss)` identity.
        target_subject: The principal to revoke the role from (`iss` implied,
            PLAN.md §0.12).
        access_role: The caller-supplied role name -- `"SystemOwner"`,
            `"SystemAdmin"`, or `"PolicyManager"`.
        store: The `AccessRoleStore` to mutate.
        issuer: This process's configured issuer, filled in as the target's
            `principal_issuer`.

    Returns:
        `RevokeResult(system_owner_floor_warning=...)` -- true when exactly
        one active `SystemOwner` exists after this revoke (AC-BI-007).

    Raises:
        InvalidAccessRoleError: `access_role` is not one of the three roles
            `revoke_role` manages (AC-BI-013).
        AccessDeniedError: `actor` does not hold the role this revoke
            requires -- one `outcome='rejected'` `access_role.revoke` audit
            event (`reason_code="access_denied"`) is recorded first
            (AC-BI-012).
        SelfGrantOrRevokeBlockedError: `actor` and the target are the same
            principal (AC-BI-005) -- likewise recorded first
            (`reason_code="self_revoke_blocked"`, AC-BI-012).
        SystemOwnerFloorViolationError: revoking `SYSTEM_OWNER` from
            `target_subject` would leave zero active `SystemOwner`s
            (AC-BI-006) -- recorded first (both the pre-mutation rule check
            and the store-level concurrent-revoke race translation use
            `reason_code="system_owner_floor_violation"`, AC-BI-012).
        AuthorizationStoreUnavailableError: propagated from any underlying
            store failure (AC-BI-011), including a failed denial-audit
            write itself (`_record_rejected_or_raise_unavailable`).
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
        except (AccessRolePostgresConnectionError, AccessRoleAssignmentPersistenceError) as exc:
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
    except (AccessRolePostgresConnectionError, AccessRoleAssignmentPersistenceError) as exc:
        raise AuthorizationStoreUnavailableError(_AUTHORIZATION_STORE_UNAVAILABLE_MESSAGE) from exc
    _maybe_log_system_owner_floor_warning("revoke_role", active_system_owners)
    return RevokeResult(system_owner_floor_warning=active_system_owners == 1)


def _validate_audit_query_filters(filters: AuditQueryFilters, *, page_size: int) -> None:
    """Raise `InvalidAuditQueryFilterError` naming the first invalid filter found (AC-BI-008).

    Runs entirely before `list_audit_events` ever calls `audit_store.query`
    -- unknown `action` (not in the typed-model registry), unknown
    `resource_type` (not in the resource-type registry), `occurred_from`
    later than `occurred_to`, and `page_size` above the configured maximum.
    `cursor` malformedness is not checked here -- `AuditStore.query` itself
    validates it (it alone knows the opaque token's internal shape);
    `list_audit_events` translates that failure separately, below.
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


def list_audit_events(
    principal: tuple[str, str],
    *,
    filters: AuditQueryFilters,
    cursor: str | None,
    page_size: int,
    access_role_store: AccessRoleStore,
    audit_store: AuditStore,
) -> AuditQueryPage:
    """Return one filtered, newest-first, paginated page of `audit_events` (AC-BI-007).

    Gated at `require_role(principal, minimum=SYSTEM_ADMIN, store=access_role_store)`
    first, mirroring `list_assignments`'s own gate-then-query shape exactly
    -- satisfies AC-BI-001/AC-BI-002's "refused... before any store call."
    `filters`/`page_size` are then validated (AC-BI-008) before
    `audit_store.query` is ever called. `cursor` is not validated here --
    `AuditStore.query` decodes and validates it itself; a malformed cursor
    surfaces as the same `InvalidAuditQueryFilterError` family, translated
    below.

    Args:
        principal: The caller's verified `(sub, iss)` identity.
        filters: Every combinable filter `list-audit-events` accepts.
        cursor: A prior page's `next_cursor`, or `None` for the first page.
        page_size: How many events to return per page (bounded by
            `_LIST_AUDIT_EVENTS_MAX_PAGE_SIZE`).
        access_role_store: The `AccessRoleStore` `require_role` resolves
            `principal`'s roles against.
        audit_store: The `AuditStore` to query.

    Returns:
        `AuditQueryPage(events=..., next_cursor=...)`, newest-first.

    Raises:
        AccessDeniedError: `principal` does not hold `SystemAdmin` or above
            (AC-BI-002).
        InvalidAuditQueryFilterError: a filter, `page_size`, or `cursor` is
            invalid (AC-BI-008) -- named in the message.
        AuthorizationStoreUnavailableError: propagated from `require_role`
            (AC-BI-011), or from `audit_store.query` failing to reach the
            authz Postgres.
    """
    require_role(principal, minimum=AccessRole.SYSTEM_ADMIN, store=access_role_store)
    _validate_audit_query_filters(filters, page_size=page_size)
    try:
        return audit_store.query(filters=filters, cursor=cursor, page_size=page_size)
    except AuditInvalidCursorError as exc:
        raise InvalidAuditQueryFilterError(_INVALID_AUDIT_CURSOR_FILTER_MESSAGE) from exc
    except (AuditPostgresUnavailableError, AuditPersistenceError) as exc:
        raise AuthorizationStoreUnavailableError(_AUTHORIZATION_STORE_UNAVAILABLE_MESSAGE) from exc
