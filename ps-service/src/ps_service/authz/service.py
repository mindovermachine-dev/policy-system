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
    SelfGrantOrRevokeBlockedError,
    SystemOwnerFloorViolationError,
)
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
    from ps_service.authz.models import AccessRoleAssignmentRow
    from ps_service.authz.store import AccessRoleStore

_ACCESS_DENIED_MESSAGE = "You do not have the required access role for this action."
_AUTHORIZATION_STORE_UNAVAILABLE_MESSAGE = "The authorization store is temporarily unavailable."
_INVALID_ACCESS_ROLE_MESSAGE = "The requested access role is not recognized."
_SELF_GRANT_OR_REVOKE_BLOCKED_MESSAGE = "You cannot grant or revoke your own access roles."
_SYSTEM_OWNER_FLOOR_VIOLATION_MESSAGE = "This action would leave zero active SystemOwners."
_SYSTEM_OWNER_FLOOR = 1

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
            requires.
        SelfGrantOrRevokeBlockedError: `actor` and the target are the same
            principal (AC-BI-005).
        AuthorizationStoreUnavailableError: propagated from any underlying
            store failure (AC-BI-011).
    """
    role = _parse_grantable_access_role(access_role, rbac=_GRANT_RBAC)
    actor_roles = resolve_active_roles(actor, store=store)
    if not (actor_roles & _GRANT_RBAC[role]):
        raise AccessDeniedError(_ACCESS_DENIED_MESSAGE)
    target = (target_subject, issuer)
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
            requires.
        SelfGrantOrRevokeBlockedError: `actor` and the target are the same
            principal (AC-BI-005).
        SystemOwnerFloorViolationError: revoking `SYSTEM_OWNER` from
            `target_subject` would leave zero active `SystemOwner`s
            (AC-BI-006).
        AuthorizationStoreUnavailableError: propagated from any underlying
            store failure (AC-BI-011).
    """
    role = _parse_grantable_access_role(access_role, rbac=_REVOKE_RBAC)
    actor_roles = resolve_active_roles(actor, store=store)
    if not (actor_roles & _REVOKE_RBAC[role]):
        raise AccessDeniedError(_ACCESS_DENIED_MESSAGE)
    target = (target_subject, issuer)
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
            raise SystemOwnerFloorViolationError(_SYSTEM_OWNER_FLOOR_VIOLATION_MESSAGE)
    try:
        store.revoke(actor=actor, target=target, access_role=role)
        active_system_owners = store.count_active_system_owners()
    except AccessRoleSystemOwnerFloorRaceError as exc:
        raise SystemOwnerFloorViolationError(_SYSTEM_OWNER_FLOOR_VIOLATION_MESSAGE) from exc
    except (AccessRolePostgresConnectionError, AccessRoleAssignmentPersistenceError) as exc:
        raise AuthorizationStoreUnavailableError(_AUTHORIZATION_STORE_UNAVAILABLE_MESSAGE) from exc
    _maybe_log_system_owner_floor_warning("revoke_role", active_system_owners)
    return RevokeResult(system_owner_floor_warning=active_system_owners == 1)
