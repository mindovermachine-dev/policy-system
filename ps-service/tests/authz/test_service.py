"""Tests for `ps_service.authz.service` (issue #133, PLAN.md §2.3, Slices 1-3).

Direct unit-level coverage of `resolve_active_roles`/`require_role`/
`list_assignments`/`grant_role`/`revoke_role` against
`FakeAccessRoleStore`/`RaisingAccessRoleStore` (`tests/authz/_fakes.py`) --
complements `tests/mcp_interface/test_access_role_tools.py`'s own
end-to-end MCP-level proof with focused, single-reason-to-fail assertions
on the service layer itself, in particular PLAN.md §0.8's hierarchy fix (a
`SystemOwner` also satisfies a `SystemAdmin` minimum check), Slice 2's own
grant/revoke RBAC + `block_self_target` + closed-set validation, and Slice
3's `SystemOwner` revoke + `enforce_system_owner_floor` (AC-BI-006) --
per CHANGES.md, the real MCP-reachable proof of AC-BI-006/AC-BI-007 is
`test_access_role_tools.py`'s own Appendix A 5-step scenario, not the
fake-store-seeded tests here, which remain as a supplementary,
narrower-scoped defensive-logic check on the service layer itself.

Collecting at least one real test file from this package also gives
`tests/mcp_interface/test_access_role_tools.py`'s own
`from authz._fakes import ...` cross-package import something to resolve
against during a full-suite run (pytest's `--import-mode=importlib` only
makes a package's submodules resolvable as real imports once pytest has
itself collected something from that package first -- the same constraint
`test_near_miss_tools.py:213-227` documents for
`passkey_signing`/`mcp_interface`).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

import ps_service.authz.audit_actions  # noqa: F401  # pyright: ignore[reportUnusedImport] -- side-effect import, registers access_role.* actions/`"principal"` resource type before this file's own filter-validation tests run in isolation
from authz._fakes import (
    FakeAccessRoleStore,
    FakeAuditStore,
    RaisingAccessRoleStore,
    RaisingAfterGateAccessRoleStore,
    RaisingAuditOnRejectAccessRoleStore,
    RejectedAuditRecord,
)
from ps_service.api.errors import (
    AccessDeniedError,
    AuthorizationStoreUnavailableError,
    InvalidAccessRoleError,
    InvalidAuditQueryFilterError,
    SelfGrantOrRevokeBlockedError,
    SystemOwnerFloorViolationError,
)
from ps_service.audit.errors import AuditInvalidCursorError, AuditPostgresUnavailableError
from ps_service.audit.models import AuditEventRow, AuditQueryFilters, AuditQueryPage
from ps_service.authz.models import AccessRole
from ps_service.authz.service import (
    grant_role,
    list_assignments,
    list_audit_events,
    require_role,
    resolve_active_roles,
    revoke_role,
)

_FIRST_CALLER = ("first-caller", "https://issuer.example.com/")
_SECOND_CALLER = ("second-caller", "https://issuer.example.com/")
_THIRD_CALLER = ("third-caller", "https://issuer.example.com/")
_ISSUER = "https://issuer.example.com/"


def test_resolve_active_roles_bootstraps_the_first_ever_principal() -> None:
    """AC-BI-001: an empty store bootstraps the calling principal to both roles."""
    store = FakeAccessRoleStore(expected_owner=_FIRST_CALLER)

    roles = resolve_active_roles(_FIRST_CALLER, store=store)

    assert roles == {AccessRole.AUTHENTICATED_USER, AccessRole.SYSTEM_OWNER}


def test_resolve_active_roles_defaults_a_later_principal_to_authenticated_user() -> None:
    """AC-BI-002: once non-empty, a different principal defaults to AuthenticatedUser only."""
    store = FakeAccessRoleStore(expected_owner=_FIRST_CALLER)
    resolve_active_roles(_FIRST_CALLER, store=store)

    roles = resolve_active_roles(_SECOND_CALLER, store=store)

    assert roles == {AccessRole.AUTHENTICATED_USER}


def test_resolve_active_roles_bootstraps_only_the_configured_owner_identity() -> None:
    """AC-BI-003: the configured expected owner still bootstraps to both roles."""
    store = FakeAccessRoleStore(expected_owner=_FIRST_CALLER)

    roles = resolve_active_roles(_FIRST_CALLER, store=store)

    assert roles == {AccessRole.AUTHENTICATED_USER, AccessRole.SYSTEM_OWNER}


def test_resolve_active_roles_rejects_a_non_matching_principal_in_an_empty_store() -> None:
    """AC-BI-004: a non-matching caller in an empty store gets no grant; the table stays empty."""
    store = FakeAccessRoleStore(expected_owner=_FIRST_CALLER)

    roles = resolve_active_roles(_SECOND_CALLER, store=store)

    assert roles == {AccessRole.AUTHENTICATED_USER}
    assert store.list_all_assignments() == ()


def test_resolve_active_roles_still_lets_the_configured_owner_bootstrap_after_a_rejected_attempt() -> (  # noqa: E501 - name mirrors PLAN.md Slice 2 verbatim
    None
):
    """A prior rejection doesn't poison the bootstrap window for the configured owner."""
    store = FakeAccessRoleStore(expected_owner=_FIRST_CALLER)
    resolve_active_roles(_SECOND_CALLER, store=store)  # rejected, no grant

    roles = resolve_active_roles(_FIRST_CALLER, store=store)

    assert roles == {AccessRole.AUTHENTICATED_USER, AccessRole.SYSTEM_OWNER}


def test_require_role_system_owner_satisfies_a_system_admin_minimum() -> None:
    """PLAN.md §0.8's hierarchy fix: the sole bootstrapped SystemOwner passes a SystemAdmin gate.

    Without this fix, AC-BI-001/AC-BI-005/AC-BI-008 jointly describe a
    permanent lockout (the sole SystemOwner could never self-grant
    SystemAdmin, so could never pass a literal SystemAdmin-only check).
    """
    store = FakeAccessRoleStore(expected_owner=_FIRST_CALLER)
    resolve_active_roles(_FIRST_CALLER, store=store)  # bootstraps -- SystemOwner only

    require_role(_FIRST_CALLER, minimum=AccessRole.SYSTEM_ADMIN, store=store)  # must not raise


def test_require_role_denies_a_principal_with_only_authenticated_user() -> None:
    """A principal holding only the AuthenticatedUser default is denied a SystemAdmin gate."""
    store = FakeAccessRoleStore(expected_owner=_FIRST_CALLER)
    resolve_active_roles(_FIRST_CALLER, store=store)  # first caller bootstraps

    with pytest.raises(AccessDeniedError) as exc_info:
        require_role(_SECOND_CALLER, minimum=AccessRole.SYSTEM_ADMIN, store=store)

    assert str(exc_info.value) == "You do not have the required access role for this action."


def test_require_role_never_lets_system_owner_satisfy_a_policy_manager_minimum_by_accident() -> (
    None
):
    """PLAN.md §0.8: the hierarchy fix is scoped to SystemAdmin only, never PolicyManager.

    Confirms the fix is not accidentally a generic "SystemOwner satisfies
    everything" rule -- a bare AuthenticatedUser-only principal (never
    granted PolicyManager) is still denied a PolicyManager gate even though
    nothing in this slice ever calls `require_role` with that minimum yet.
    """
    store = FakeAccessRoleStore(expected_owner=_FIRST_CALLER)
    resolve_active_roles(_FIRST_CALLER, store=store)  # bootstraps -- SystemOwner, not PolicyManager

    with pytest.raises(AccessDeniedError):
        require_role(_FIRST_CALLER, minimum=AccessRole.POLICY_MANAGER, store=store)


def test_require_role_never_lets_system_owner_satisfy_compliance_officer_minimum_by_accident() -> (
    None
):
    """Issue #145 Slice 1, mirroring the PolicyManager non-hierarchy proof above.

    Confirms the SystemAdmin-only hierarchy fix does not accidentally extend
    to ComplianceOfficer either -- a bare AuthenticatedUser-only principal
    (never granted ComplianceOfficer) is still denied a ComplianceOfficer
    gate even though nothing in this slice ever calls `require_role` with
    that minimum yet.
    """
    store = FakeAccessRoleStore(expected_owner=_FIRST_CALLER)
    resolve_active_roles(_FIRST_CALLER, store=store)  # bootstraps SystemOwner only

    with pytest.raises(AccessDeniedError):
        require_role(_FIRST_CALLER, minimum=AccessRole.COMPLIANCE_OFFICER, store=store)


def test_list_assignments_returns_every_row_and_the_floor_warning() -> None:
    """AC-BI-007's floor condition: with exactly one active SystemOwner, the warning is true."""
    store = FakeAccessRoleStore(expected_owner=_FIRST_CALLER)
    resolve_active_roles(_FIRST_CALLER, store=store)

    result = list_assignments(_FIRST_CALLER, store=store)

    assert {row.access_role for row in result.assignments} == {
        AccessRole.AUTHENTICATED_USER,
        AccessRole.SYSTEM_OWNER,
    }
    assert result.system_owner_floor_warning is True


def test_list_assignments_denies_a_caller_without_system_admin_or_above() -> None:
    """§0.9's plan-original list-gate: a bare AuthenticatedUser caller is denied the roster."""
    store = FakeAccessRoleStore(expected_owner=_FIRST_CALLER)
    resolve_active_roles(_FIRST_CALLER, store=store)  # first caller bootstraps, store now non-empty

    with pytest.raises(AccessDeniedError):
        list_assignments(_SECOND_CALLER, store=store)


def test_resolve_active_roles_never_falls_open_on_a_store_connection_error() -> None:
    """AC-BI-011: a simulated store outage raises, never silently bootstraps/defaults."""
    store = RaisingAccessRoleStore()

    with pytest.raises(AuthorizationStoreUnavailableError) as exc_info:
        resolve_active_roles(_FIRST_CALLER, store=store)

    assert str(exc_info.value) == "The authorization store is temporarily unavailable."


def test_list_assignments_never_falls_open_on_a_store_connection_error() -> None:
    """AC-BI-011: a store outage surfacing at the RBAC gate itself still fails closed."""
    store = RaisingAccessRoleStore()

    with pytest.raises(AuthorizationStoreUnavailableError):
        list_assignments(_FIRST_CALLER, store=store)


def test_list_assignments_never_falls_open_on_a_store_outage_discovered_after_the_gate_passes() -> (
    None
):
    """AC-BI-011: a store outage discovered only in the roster read (after the RBAC gate already
    passed) still fails closed, exercising `list_assignments`'s second, independent try/except.
    """
    store = RaisingAfterGateAccessRoleStore(expected_owner=_FIRST_CALLER)
    store.bootstrap_first_owner(_FIRST_CALLER)  # succeeds -- FIRST_CALLER now holds SystemOwner

    with pytest.raises(AuthorizationStoreUnavailableError):
        list_assignments(_FIRST_CALLER, store=store)


# --- grant_role / revoke_role (Slice 2, PLAN.md §4) --------------------------


def test_grant_role_lets_the_bootstrapped_system_owner_grant_system_admin() -> None:
    """AC-BI-003/AC-BI-015: SystemOwner grants SystemAdmin; one audit event is recorded."""
    store = FakeAccessRoleStore(expected_owner=_FIRST_CALLER)
    resolve_active_roles(_FIRST_CALLER, store=store)  # bootstraps FIRST_CALLER as SystemOwner

    result = grant_role(
        actor=_FIRST_CALLER,
        target_subject=_SECOND_CALLER[0],
        access_role="SystemAdmin",
        store=store,
        issuer=_ISSUER,
    )

    assert result.system_owner_floor_warning is True  # still exactly one SystemOwner
    assert AccessRole.SYSTEM_ADMIN in store.active_roles_for(_SECOND_CALLER)
    grant_events = [event for event in store._events if event.event_type == "grant"]  # pyright: ignore[reportPrivateUsage]  -- test-only fake, direct-field audit-trail assertion mirrors the codebase's own established test convention
    assert len(grant_events) == 1
    event = grant_events[0]
    assert event.actor_subject == _FIRST_CALLER[0]
    assert event.target_subject == _SECOND_CALLER[0]
    assert event.access_role is AccessRole.SYSTEM_ADMIN
    assert event.occurred_at is not None


def test_grant_role_lets_a_system_admin_grant_policy_manager() -> None:
    """AC-BI-004: a SystemAdmin (not just a SystemOwner) may grant PolicyManager."""
    store = FakeAccessRoleStore(expected_owner=_FIRST_CALLER)
    resolve_active_roles(_FIRST_CALLER, store=store)  # bootstraps FIRST_CALLER as SystemOwner
    grant_role(
        actor=_FIRST_CALLER,
        target_subject=_SECOND_CALLER[0],
        access_role="SystemAdmin",
        store=store,
        issuer=_ISSUER,
    )

    result = grant_role(
        actor=_SECOND_CALLER,
        target_subject=_THIRD_CALLER[0],
        access_role="PolicyManager",
        store=store,
        issuer=_ISSUER,
    )

    assert AccessRole.POLICY_MANAGER in store.active_roles_for(_THIRD_CALLER)
    assert result.system_owner_floor_warning is True


def test_grant_role_lets_the_system_owner_grant_policy_manager_directly() -> None:
    """AC-BI-004: a SystemOwner may also grant PolicyManager directly."""
    store = FakeAccessRoleStore(expected_owner=_FIRST_CALLER)
    resolve_active_roles(_FIRST_CALLER, store=store)

    grant_role(
        actor=_FIRST_CALLER,
        target_subject=_SECOND_CALLER[0],
        access_role="PolicyManager",
        store=store,
        issuer=_ISSUER,
    )

    assert AccessRole.POLICY_MANAGER in store.active_roles_for(_SECOND_CALLER)


def test_grant_role_lets_a_system_admin_grant_compliance_officer() -> None:
    """Issue #145 Slice 1: ComplianceOfficer's grant RBAC mirrors PolicyManager's own row --
    a SystemAdmin (not just a SystemOwner) may grant it.
    """
    store = FakeAccessRoleStore(expected_owner=_FIRST_CALLER)
    resolve_active_roles(_FIRST_CALLER, store=store)  # bootstraps FIRST_CALLER as SystemOwner
    grant_role(
        actor=_FIRST_CALLER,
        target_subject=_SECOND_CALLER[0],
        access_role="SystemAdmin",
        store=store,
        issuer=_ISSUER,
    )

    result = grant_role(
        actor=_SECOND_CALLER,
        target_subject=_THIRD_CALLER[0],
        access_role="ComplianceOfficer",
        store=store,
        issuer=_ISSUER,
    )

    assert AccessRole.COMPLIANCE_OFFICER in store.active_roles_for(_THIRD_CALLER)
    assert result.system_owner_floor_warning is True


def test_grant_role_lets_the_system_owner_grant_compliance_officer_directly() -> None:
    """Issue #145 Slice 1: a SystemOwner may also grant ComplianceOfficer directly."""
    store = FakeAccessRoleStore(expected_owner=_FIRST_CALLER)
    resolve_active_roles(_FIRST_CALLER, store=store)

    grant_role(
        actor=_FIRST_CALLER,
        target_subject=_SECOND_CALLER[0],
        access_role="ComplianceOfficer",
        store=store,
        issuer=_ISSUER,
    )

    assert AccessRole.COMPLIANCE_OFFICER in store.active_roles_for(_SECOND_CALLER)


def test_grant_role_lets_the_system_owner_grant_a_peer_system_owner() -> None:
    """CHANGES.md MAJOR resolution: SystemOwner may grant SystemOwner to a peer.

    Both principals show as SystemOwner afterwards -- this is the new flow
    that makes AC-BI-006/AC-BI-007 genuinely MCP-reachable (Appendix A).
    """
    store = FakeAccessRoleStore(expected_owner=_FIRST_CALLER)
    resolve_active_roles(_FIRST_CALLER, store=store)
    assert store.count_active_system_owners() == 1

    result = grant_role(
        actor=_FIRST_CALLER,
        target_subject=_SECOND_CALLER[0],
        access_role="SystemOwner",
        store=store,
        issuer=_ISSUER,
    )

    assert AccessRole.SYSTEM_OWNER in store.active_roles_for(_SECOND_CALLER)
    assert store.count_active_system_owners() == 2
    assert result.system_owner_floor_warning is False  # two owners now -- no longer at the floor


def test_grant_role_rejects_a_non_owner_granting_system_owner() -> None:
    """CHANGES.md Appendix A: only a SystemOwner may grant SystemOwner -- a SystemAdmin cannot."""
    store = FakeAccessRoleStore(expected_owner=_FIRST_CALLER)
    resolve_active_roles(_FIRST_CALLER, store=store)  # FIRST_CALLER: SystemOwner
    grant_role(
        actor=_FIRST_CALLER,
        target_subject=_SECOND_CALLER[0],
        access_role="SystemAdmin",
        store=store,
        issuer=_ISSUER,
    )  # SECOND_CALLER: SystemAdmin only

    with pytest.raises(AccessDeniedError):
        grant_role(
            actor=_SECOND_CALLER,
            target_subject=_THIRD_CALLER[0],
            access_role="SystemOwner",
            store=store,
            issuer=_ISSUER,
        )
    assert AccessRole.SYSTEM_OWNER not in store.active_roles_for(_THIRD_CALLER)


def test_grant_role_rejects_a_non_owner_granting_system_admin() -> None:
    """PLAN.md §0.7: granting SystemAdmin requires the actor hold SystemOwner, not merely exist.

    AC-BI-012: this denial also records one `outcome='rejected'`
    `access_role.grant` audit event naming the attempting actor, the
    target, and `reason_code="access_denied"`.
    """
    store = FakeAccessRoleStore(expected_owner=_FIRST_CALLER)
    resolve_active_roles(_FIRST_CALLER, store=store)  # bootstraps
    resolve_active_roles(_SECOND_CALLER, store=store)  # defaults to AuthenticatedUser only

    with pytest.raises(AccessDeniedError) as exc_info:
        grant_role(
            actor=_SECOND_CALLER,
            target_subject=_THIRD_CALLER[0],
            access_role="SystemAdmin",
            store=store,
            issuer=_ISSUER,
        )
    assert str(exc_info.value) == "You do not have the required access role for this action."
    assert store.rejected_records == [
        RejectedAuditRecord(
            action="grant",
            actor=_SECOND_CALLER,
            target=(_THIRD_CALLER[0], _ISSUER),
            access_role=AccessRole.SYSTEM_ADMIN,
            reason_code="access_denied",
        )
    ]


def test_grant_role_lets_a_system_owner_grant_themselves_another_role() -> None:
    """AC-BI-005 exemption: a SystemOwner may grant any role to their own subject."""
    store = FakeAccessRoleStore(expected_owner=_FIRST_CALLER)
    resolve_active_roles(_FIRST_CALLER, store=store)

    grant_role(
        actor=_FIRST_CALLER,
        target_subject=_FIRST_CALLER[0],
        access_role="PolicyManager",
        store=store,
        issuer=_ISSUER,
    )

    assert AccessRole.POLICY_MANAGER in store.active_roles_for(_FIRST_CALLER)
    assert store.rejected_records == []


def test_grant_role_still_blocks_a_non_owner_granting_themselves_a_role() -> None:
    """AC-BI-005 still binds everyone but SystemOwner: a SystemAdmin may not self-grant.

    AC-BI-012: this denial also records one `outcome='rejected'`
    `access_role.grant` audit event with `reason_code="self_grant_blocked"`.
    """
    store = FakeAccessRoleStore(expected_owner=_FIRST_CALLER)
    resolve_active_roles(_FIRST_CALLER, store=store)
    grant_role(
        actor=_FIRST_CALLER,
        target_subject=_SECOND_CALLER[0],
        access_role="SystemAdmin",
        store=store,
        issuer=_ISSUER,
    )

    with pytest.raises(SelfGrantOrRevokeBlockedError) as exc_info:
        grant_role(
            actor=_SECOND_CALLER,
            target_subject=_SECOND_CALLER[0],
            access_role="PolicyManager",
            store=store,
            issuer=_ISSUER,
        )
    assert str(exc_info.value) == "You cannot grant or revoke your own access roles."
    assert AccessRole.POLICY_MANAGER not in store.active_roles_for(_SECOND_CALLER)
    assert store.rejected_records == [
        RejectedAuditRecord(
            action="grant",
            actor=_SECOND_CALLER,
            target=(_SECOND_CALLER[0], _ISSUER),
            access_role=AccessRole.POLICY_MANAGER,
            reason_code="self_grant_blocked",
        )
    ]


def test_grant_role_converts_a_failed_denial_audit_write_into_authorization_store_unavailable() -> (
    None
):
    """AC-BI-011 applied to the denial-recording path (CHANGES.md item 3, PLAN.md §4 Slice 3).

    When the audit write for a denial itself fails (Postgres unreachable),
    the caller must not see the original `AccessDeniedError` -- a denial
    that cannot be proven to have been durably logged is not a safe
    "denied" response; `AuthorizationStoreUnavailableError` is raised
    instead, and its message leaks no host/port/driver detail.
    """
    store = RaisingAuditOnRejectAccessRoleStore(expected_owner=_FIRST_CALLER)
    resolve_active_roles(_FIRST_CALLER, store=store)  # bootstraps
    resolve_active_roles(_SECOND_CALLER, store=store)  # defaults to AuthenticatedUser only

    with pytest.raises(AuthorizationStoreUnavailableError) as exc_info:
        grant_role(
            actor=_SECOND_CALLER,
            target_subject=_THIRD_CALLER[0],
            access_role="SystemAdmin",
            store=store,
            issuer=_ISSUER,
        )
    message = str(exc_info.value)
    assert message == "The authorization store is temporarily unavailable."
    assert "host" not in message.lower()
    assert "port" not in message.lower()
    assert "psycopg" not in message.lower()


def test_grant_role_rejects_an_access_role_outside_the_closed_set() -> None:
    """AC-BI-013 (inner layer): a raw string naming no real AccessRole is rejected, no row written.

    `AccessRole("SuperAdmin")` itself raises `ValueError` (PLAN.md §0.6) --
    `grant_role` is the one place that catches it and re-raises the
    domain-specific `InvalidAccessRoleError`, exercised here via a direct
    call that bypasses the MCP tool's own `Literal[...]` schema entirely.
    """
    store = FakeAccessRoleStore(expected_owner=_FIRST_CALLER)
    resolve_active_roles(_FIRST_CALLER, store=store)

    with pytest.raises(InvalidAccessRoleError) as exc_info:
        grant_role(
            actor=_FIRST_CALLER,
            target_subject=_SECOND_CALLER[0],
            access_role="SuperAdmin",
            store=store,
            issuer=_ISSUER,
        )
    assert str(exc_info.value) == "The requested access role is not recognized."
    # No row was ever written for the rejected target -- only the bootstrap
    # rows (for FIRST_CALLER, the actor) exist.
    assert len(store.list_all_assignments()) == 2
    assert {row.principal_subject for row in store.list_all_assignments()} == {_FIRST_CALLER[0]}


def test_grant_role_rejects_authenticated_user_as_a_grant_target() -> None:
    """AC-BI-013: `AuthenticatedUser` is a valid AccessRole but never grantable via this flow."""
    store = FakeAccessRoleStore(expected_owner=_FIRST_CALLER)
    resolve_active_roles(_FIRST_CALLER, store=store)

    with pytest.raises(InvalidAccessRoleError):
        grant_role(
            actor=_FIRST_CALLER,
            target_subject=_SECOND_CALLER[0],
            access_role="AuthenticatedUser",
            store=store,
            issuer=_ISSUER,
        )


def test_grant_role_never_falls_open_on_a_store_connection_error() -> None:
    """AC-BI-011: a simulated store outage during the mutation itself fails closed."""
    store = RaisingAfterGateAccessRoleStore(expected_owner=_FIRST_CALLER)
    store.bootstrap_first_owner(_FIRST_CALLER)

    with pytest.raises(AuthorizationStoreUnavailableError):
        grant_role(
            actor=_FIRST_CALLER,
            target_subject=_SECOND_CALLER[0],
            access_role="SystemAdmin",
            store=store,
            issuer=_ISSUER,
        )


def test_revoke_role_round_trips_system_admin_with_its_own_audit_event() -> None:
    """AC-BI-015: revoking SystemAdmin deletes the row and records one 'revoke' audit event."""
    store = FakeAccessRoleStore(expected_owner=_FIRST_CALLER)
    resolve_active_roles(_FIRST_CALLER, store=store)
    grant_role(
        actor=_FIRST_CALLER,
        target_subject=_SECOND_CALLER[0],
        access_role="SystemAdmin",
        store=store,
        issuer=_ISSUER,
    )

    result = revoke_role(
        actor=_FIRST_CALLER,
        target_subject=_SECOND_CALLER[0],
        access_role="SystemAdmin",
        store=store,
        issuer=_ISSUER,
    )

    assert AccessRole.SYSTEM_ADMIN not in store.active_roles_for(_SECOND_CALLER)
    assert result.system_owner_floor_warning is True
    revoke_events = [event for event in store._events if event.event_type == "revoke"]  # pyright: ignore[reportPrivateUsage]  -- test-only fake, direct-field audit-trail assertion mirrors the codebase's own established test convention
    assert len(revoke_events) == 1
    event = revoke_events[0]
    assert event.actor_subject == _FIRST_CALLER[0]
    assert event.target_subject == _SECOND_CALLER[0]
    assert event.access_role is AccessRole.SYSTEM_ADMIN


def test_revoke_role_round_trips_policy_manager() -> None:
    """AC-BI-004/015 symmetry: PolicyManager also revokes cleanly, with its own audit event."""
    store = FakeAccessRoleStore(expected_owner=_FIRST_CALLER)
    resolve_active_roles(_FIRST_CALLER, store=store)
    grant_role(
        actor=_FIRST_CALLER,
        target_subject=_SECOND_CALLER[0],
        access_role="PolicyManager",
        store=store,
        issuer=_ISSUER,
    )

    revoke_role(
        actor=_FIRST_CALLER,
        target_subject=_SECOND_CALLER[0],
        access_role="PolicyManager",
        store=store,
        issuer=_ISSUER,
    )

    assert AccessRole.POLICY_MANAGER not in store.active_roles_for(_SECOND_CALLER)


def test_revoke_role_round_trips_compliance_officer() -> None:
    """Issue #145 Slice 1: ComplianceOfficer revokes cleanly, mirroring PolicyManager's own test."""
    store = FakeAccessRoleStore(expected_owner=_FIRST_CALLER)
    resolve_active_roles(_FIRST_CALLER, store=store)
    grant_role(
        actor=_FIRST_CALLER,
        target_subject=_SECOND_CALLER[0],
        access_role="ComplianceOfficer",
        store=store,
        issuer=_ISSUER,
    )

    revoke_role(
        actor=_FIRST_CALLER,
        target_subject=_SECOND_CALLER[0],
        access_role="ComplianceOfficer",
        store=store,
        issuer=_ISSUER,
    )

    assert AccessRole.COMPLIANCE_OFFICER not in store.active_roles_for(_SECOND_CALLER)


def test_revoke_role_lets_a_system_owner_revoke_a_non_owner_role_from_themselves() -> None:
    """AC-BI-005 exemption extended to revoke: a SystemOwner may drop their own non-owner roles."""
    store = FakeAccessRoleStore(expected_owner=_FIRST_CALLER)
    resolve_active_roles(_FIRST_CALLER, store=store)  # bootstraps -- FIRST_CALLER: SystemOwner
    grant_role(
        actor=_FIRST_CALLER,
        target_subject=_FIRST_CALLER[0],
        access_role="SystemAdmin",
        store=store,
        issuer=_ISSUER,
    )

    revoke_role(
        actor=_FIRST_CALLER,
        target_subject=_FIRST_CALLER[0],
        access_role="SystemAdmin",
        store=store,
        issuer=_ISSUER,
    )

    assert AccessRole.SYSTEM_ADMIN not in store.active_roles_for(_FIRST_CALLER)
    assert AccessRole.SYSTEM_OWNER in store.active_roles_for(_FIRST_CALLER)
    assert store.rejected_records == []


def test_revoke_role_denies_a_bare_system_admin_who_is_not_rbac_eligible_at_all() -> None:
    """A plain SystemAdmin actor is never authorized to revoke SystemAdmin -- not even their own.

    This is `AccessDeniedError`, not `SelfGrantOrRevokeBlockedError`: RBAC
    is checked before the self-target rule (mirroring `grant_role`'s own
    order, PLAN.md §2.3), so an actor who could never revoke this role from
    *anyone* is turned away by RBAC first, self-target or not.

    AC-BI-012: this denial also records one `outcome='rejected'`
    `access_role.revoke` audit event with `reason_code="access_denied"`.
    """
    store = FakeAccessRoleStore(expected_owner=_FIRST_CALLER)
    resolve_active_roles(_FIRST_CALLER, store=store)
    grant_role(
        actor=_FIRST_CALLER,
        target_subject=_SECOND_CALLER[0],
        access_role="SystemAdmin",
        store=store,
        issuer=_ISSUER,
    )

    with pytest.raises(AccessDeniedError):
        revoke_role(
            actor=_SECOND_CALLER,
            target_subject=_SECOND_CALLER[0],
            access_role="SystemAdmin",
            store=store,
            issuer=_ISSUER,
        )
    assert AccessRole.SYSTEM_ADMIN in store.active_roles_for(_SECOND_CALLER)
    assert store.rejected_records == [
        RejectedAuditRecord(
            action="revoke",
            actor=_SECOND_CALLER,
            target=(_SECOND_CALLER[0], _ISSUER),
            access_role=AccessRole.SYSTEM_ADMIN,
            reason_code="access_denied",
        )
    ]


def test_revoke_role_rejects_a_non_owner_non_admin_actor() -> None:
    """Revoke RBAC mirrors grant's own actor requirement (a documented judgment call)."""
    store = FakeAccessRoleStore(expected_owner=_FIRST_CALLER)
    resolve_active_roles(_FIRST_CALLER, store=store)
    grant_role(
        actor=_FIRST_CALLER,
        target_subject=_SECOND_CALLER[0],
        access_role="PolicyManager",
        store=store,
        issuer=_ISSUER,
    )
    resolve_active_roles(_THIRD_CALLER, store=store)  # defaults to AuthenticatedUser only

    with pytest.raises(AccessDeniedError):
        revoke_role(
            actor=_THIRD_CALLER,
            target_subject=_SECOND_CALLER[0],
            access_role="PolicyManager",
            store=store,
            issuer=_ISSUER,
        )


def test_revoke_role_rejects_an_access_role_outside_the_closed_set() -> None:
    """AC-BI-013 (inner layer), revoke side: an unrecognized role name is rejected."""
    store = FakeAccessRoleStore(expected_owner=_FIRST_CALLER)
    resolve_active_roles(_FIRST_CALLER, store=store)

    with pytest.raises(InvalidAccessRoleError) as exc_info:
        revoke_role(
            actor=_FIRST_CALLER,
            target_subject=_SECOND_CALLER[0],
            access_role="SuperOwner",
            store=store,
            issuer=_ISSUER,
        )
    assert str(exc_info.value) == "The requested access role is not recognized."


def test_revoke_role_lets_a_system_admin_revoke_system_owner_from_a_peer_owner() -> None:
    """CHANGES.md Appendix A: the widened revoke-SystemOwner RBAC -- a SystemAdmin (not just a
    SystemOwner) may revoke SystemOwner from someone else, once a second owner exists.

    This is the flow that makes AC-BI-006/AC-BI-007 genuinely MCP-reachable
    -- see `tests/mcp_interface/test_access_role_tools.py`'s own
    `test_appendix_a_five_step_multi_owner_scenario_proves_ac_bi_006_and_007`
    for the full, real MCP-level round trip this unit test's own service-layer
    slice mirrors.
    """
    store = FakeAccessRoleStore(expected_owner=_FIRST_CALLER)
    resolve_active_roles(_FIRST_CALLER, store=store)  # FIRST_CALLER: SystemOwner
    grant_role(
        actor=_FIRST_CALLER,
        target_subject=_SECOND_CALLER[0],
        access_role="SystemOwner",
        store=store,
        issuer=_ISSUER,
    )  # SECOND_CALLER: SystemOwner too -- two owners now
    grant_role(
        actor=_FIRST_CALLER,
        target_subject=_THIRD_CALLER[0],
        access_role="SystemAdmin",
        store=store,
        issuer=_ISSUER,
    )  # THIRD_CALLER: SystemAdmin only
    assert store.count_active_system_owners() == 2

    result = revoke_role(
        actor=_THIRD_CALLER,
        target_subject=_FIRST_CALLER[0],
        access_role="SystemOwner",
        store=store,
        issuer=_ISSUER,
    )

    assert AccessRole.SYSTEM_OWNER not in store.active_roles_for(_FIRST_CALLER)
    assert AccessRole.SYSTEM_OWNER in store.active_roles_for(_SECOND_CALLER)
    assert store.count_active_system_owners() == 1
    assert result.system_owner_floor_warning is True  # back down to the floor


def test_revoke_role_rejects_a_bare_policy_manager_revoking_system_owner() -> None:
    """CHANGES.md Appendix A: the widened RBAC is still a closed set -- a PolicyManager
    (neither SystemOwner nor SystemAdmin) may never revoke SystemOwner from anyone.
    """
    store = FakeAccessRoleStore(expected_owner=_FIRST_CALLER)
    resolve_active_roles(_FIRST_CALLER, store=store)
    grant_role(
        actor=_FIRST_CALLER,
        target_subject=_SECOND_CALLER[0],
        access_role="SystemOwner",
        store=store,
        issuer=_ISSUER,
    )
    grant_role(
        actor=_FIRST_CALLER,
        target_subject=_THIRD_CALLER[0],
        access_role="PolicyManager",
        store=store,
        issuer=_ISSUER,
    )

    with pytest.raises(AccessDeniedError):
        revoke_role(
            actor=_THIRD_CALLER,
            target_subject=_FIRST_CALLER[0],
            access_role="SystemOwner",
            store=store,
            issuer=_ISSUER,
        )
    assert AccessRole.SYSTEM_OWNER in store.active_roles_for(_FIRST_CALLER)


def test_revoke_role_blocks_the_sole_owner_self_revoking_before_the_floor_check_ever_runs() -> None:
    """AC-BI-005 extended to SystemOwner + PLAN.md §4 Slice 3's explicit ordering requirement.

    The sole SystemOwner is RBAC-eligible to revoke SystemOwner (their own
    role satisfies the widened `_REVOKE_RBAC[SYSTEM_OWNER]`) *and* is the
    one remaining owner (the floor check would also reject this target) --
    `SelfGrantOrRevokeBlockedError`, not `SystemOwnerFloorViolationError`,
    is what actually fires, confirming `block_self_target` is evaluated
    strictly before `enforce_system_owner_floor` in `revoke_role`'s own
    check order.
    """
    store = FakeAccessRoleStore(expected_owner=_FIRST_CALLER)
    resolve_active_roles(_FIRST_CALLER, store=store)  # bootstraps -- sole SystemOwner

    with pytest.raises(SelfGrantOrRevokeBlockedError) as exc_info:
        revoke_role(
            actor=_FIRST_CALLER,
            target_subject=_FIRST_CALLER[0],
            access_role="SystemOwner",
            store=store,
            issuer=_ISSUER,
        )
    assert str(exc_info.value) == "You cannot grant or revoke your own access roles."
    assert store.count_active_system_owners() == 1
    assert AccessRole.SYSTEM_OWNER in store.active_roles_for(_FIRST_CALLER)


def test_revoke_role_rejects_revoking_the_last_remaining_system_owner() -> None:
    """AC-BI-006: revoking the sole remaining active SystemOwner is rejected.

    A `SystemAdmin` distinct from the target (never self-targeting, so
    `block_self_target` never fires here) is the actor -- isolating
    `enforce_system_owner_floor`'s own rejection as PLAN.md §0.9 originally
    specified, now reachable for real via CHANGES.md's widened RBAC (see
    the MCP-level Appendix A test for the full real round trip that first
    brings the count down to exactly one via a genuine revoke, rather than
    seeding it directly).

    AC-BI-012: this denial also records one `outcome='rejected'`
    `access_role.revoke` audit event with
    `reason_code="system_owner_floor_violation"`.
    """
    store = FakeAccessRoleStore(expected_owner=_FIRST_CALLER)
    resolve_active_roles(_FIRST_CALLER, store=store)  # FIRST_CALLER: SystemOwner
    grant_role(
        actor=_FIRST_CALLER,
        target_subject=_THIRD_CALLER[0],
        access_role="SystemAdmin",
        store=store,
        issuer=_ISSUER,
    )  # THIRD_CALLER: SystemAdmin
    assert store.count_active_system_owners() == 1

    with pytest.raises(SystemOwnerFloorViolationError) as exc_info:
        revoke_role(
            actor=_THIRD_CALLER,
            target_subject=_FIRST_CALLER[0],
            access_role="SystemOwner",
            store=store,
            issuer=_ISSUER,
        )
    assert str(exc_info.value) == "This action would leave zero active SystemOwners."
    assert store.count_active_system_owners() == 1
    assert store.rejected_records == [
        RejectedAuditRecord(
            action="revoke",
            actor=_THIRD_CALLER,
            target=(_FIRST_CALLER[0], _ISSUER),
            access_role=AccessRole.SYSTEM_OWNER,
            reason_code="system_owner_floor_violation",
        )
    ]


def test_revoke_role_converts_a_failed_denial_audit_write_into_unavailable_error() -> None:
    """AC-BI-011 applied to the denial-recording path (CHANGES.md item 3, PLAN.md §4 Slice 3).

    Mirrors the grant-side test of the same name: when the audit write for
    a revoke denial itself fails, `AuthorizationStoreUnavailableError`
    replaces the original `SystemOwnerFloorViolationError`, with a message
    that leaks no host/port/driver detail.
    """
    store = RaisingAuditOnRejectAccessRoleStore(expected_owner=_FIRST_CALLER)
    resolve_active_roles(_FIRST_CALLER, store=store)  # FIRST_CALLER: SystemOwner
    grant_role(
        actor=_FIRST_CALLER,
        target_subject=_THIRD_CALLER[0],
        access_role="SystemAdmin",
        store=store,
        issuer=_ISSUER,
    )

    with pytest.raises(AuthorizationStoreUnavailableError) as exc_info:
        revoke_role(
            actor=_THIRD_CALLER,
            target_subject=_FIRST_CALLER[0],
            access_role="SystemOwner",
            store=store,
            issuer=_ISSUER,
        )
    message = str(exc_info.value)
    assert message == "The authorization store is temporarily unavailable."
    assert "host" not in message.lower()
    assert "port" not in message.lower()
    assert "psycopg" not in message.lower()
    assert AccessRole.SYSTEM_OWNER in store.active_roles_for(_FIRST_CALLER)


def test_revoke_role_rejects_super_owner_outside_the_closed_set() -> None:
    """AC-BI-013 (inner layer): `"SuperOwner"` is rejected the same two-layer way as Slice 2."""
    store = FakeAccessRoleStore(expected_owner=_FIRST_CALLER)
    resolve_active_roles(_FIRST_CALLER, store=store)

    with pytest.raises(InvalidAccessRoleError) as exc_info:
        revoke_role(
            actor=_FIRST_CALLER,
            target_subject=_SECOND_CALLER[0],
            access_role="SuperOwner",
            store=store,
            issuer=_ISSUER,
        )
    assert str(exc_info.value) == "The requested access role is not recognized."


def test_revoke_role_never_falls_open_on_a_store_connection_error() -> None:
    """AC-BI-011: a simulated store outage during the revoke mutation itself fails closed."""
    store = RaisingAfterGateAccessRoleStore(expected_owner=_FIRST_CALLER)
    store.bootstrap_first_owner(_FIRST_CALLER)

    with pytest.raises(AuthorizationStoreUnavailableError):
        revoke_role(
            actor=_FIRST_CALLER,
            target_subject=_SECOND_CALLER[0],
            access_role="SystemAdmin",
            store=store,
            issuer=_ISSUER,
        )


# --- list_audit_events (issue #147, Slice 4) --------------------------------


def test_list_audit_events_denies_a_caller_without_system_admin_or_above_before_any_query() -> None:
    """AC-BI-002: a bare AuthenticatedUser caller is denied, and `audit_store.query` never ran."""
    access_role_store = FakeAccessRoleStore(expected_owner=_FIRST_CALLER)
    resolve_active_roles(_FIRST_CALLER, store=access_role_store)  # bootstraps FIRST_CALLER only
    audit_store = FakeAuditStore()

    with pytest.raises(AccessDeniedError) as exc_info:
        list_audit_events(
            _SECOND_CALLER,
            filters=AuditQueryFilters(),
            cursor=None,
            page_size=25,
            access_role_store=access_role_store,
            audit_store=audit_store,
        )

    assert str(exc_info.value) == "You do not have the required access role for this action."
    assert audit_store.query_calls == []


def test_list_audit_events_never_falls_open_on_an_access_role_store_connection_error() -> None:
    """AC-BI-011 (gate half): an authz-store outage at the RBAC gate itself fails closed."""
    audit_store = FakeAuditStore()

    with pytest.raises(AuthorizationStoreUnavailableError):
        list_audit_events(
            _FIRST_CALLER,
            filters=AuditQueryFilters(),
            cursor=None,
            page_size=25,
            access_role_store=RaisingAccessRoleStore(),
            audit_store=audit_store,
        )

    assert audit_store.query_calls == []


def test_list_audit_events_rejects_an_unregistered_action_filter_before_any_query() -> None:
    """AC-BI-008: an unknown `action` filter is rejected, naming 'action', query never ran."""
    access_role_store = FakeAccessRoleStore(expected_owner=_FIRST_CALLER)
    resolve_active_roles(_FIRST_CALLER, store=access_role_store)
    audit_store = FakeAuditStore()

    with pytest.raises(InvalidAuditQueryFilterError) as exc_info:
        list_audit_events(
            _FIRST_CALLER,
            filters=AuditQueryFilters(action="nonexistent.action.never_registered"),
            cursor=None,
            page_size=25,
            access_role_store=access_role_store,
            audit_store=audit_store,
        )

    assert "action" in str(exc_info.value)
    assert audit_store.query_calls == []


def test_list_audit_events_rejects_an_unregistered_resource_type_filter_before_any_query() -> None:
    """AC-BI-008: an unknown `resource_type` filter is rejected, naming 'resource_type'."""
    access_role_store = FakeAccessRoleStore(expected_owner=_FIRST_CALLER)
    resolve_active_roles(_FIRST_CALLER, store=access_role_store)
    audit_store = FakeAuditStore()

    with pytest.raises(InvalidAuditQueryFilterError) as exc_info:
        list_audit_events(
            _FIRST_CALLER,
            filters=AuditQueryFilters(resource_type="nonexistent_resource_type"),
            cursor=None,
            page_size=25,
            access_role_store=access_role_store,
            audit_store=audit_store,
        )

    assert "resource_type" in str(exc_info.value)
    assert audit_store.query_calls == []


def test_list_audit_events_rejects_a_from_later_than_to_time_range_before_any_query() -> None:
    """AC-BI-008: `occurred_from` later than `occurred_to` is rejected."""
    access_role_store = FakeAccessRoleStore(expected_owner=_FIRST_CALLER)
    resolve_active_roles(_FIRST_CALLER, store=access_role_store)
    audit_store = FakeAuditStore()
    filters = AuditQueryFilters(
        occurred_from=datetime(2026, 1, 2, tzinfo=UTC),
        occurred_to=datetime(2026, 1, 1, tzinfo=UTC),
    )

    with pytest.raises(InvalidAuditQueryFilterError) as exc_info:
        list_audit_events(
            _FIRST_CALLER,
            filters=filters,
            cursor=None,
            page_size=25,
            access_role_store=access_role_store,
            audit_store=audit_store,
        )

    assert "occurred_from" in str(exc_info.value)
    assert audit_store.query_calls == []


def test_list_audit_events_rejects_a_page_size_above_the_maximum_before_any_query() -> None:
    """AC-BI-008: `page_size` above the configured maximum is rejected."""
    access_role_store = FakeAccessRoleStore(expected_owner=_FIRST_CALLER)
    resolve_active_roles(_FIRST_CALLER, store=access_role_store)
    audit_store = FakeAuditStore()

    with pytest.raises(InvalidAuditQueryFilterError) as exc_info:
        list_audit_events(
            _FIRST_CALLER,
            filters=AuditQueryFilters(),
            cursor=None,
            page_size=101,
            access_role_store=access_role_store,
            audit_store=audit_store,
        )

    assert "page_size" in str(exc_info.value)
    assert audit_store.query_calls == []


def test_list_audit_events_returns_the_audit_store_result_after_a_successful_gate() -> None:
    """A SystemAdmin/SystemOwner caller with valid filters gets `query`'s own page back."""
    access_role_store = FakeAccessRoleStore(expected_owner=_FIRST_CALLER)
    resolve_active_roles(_FIRST_CALLER, store=access_role_store)
    sample_event = AuditEventRow(
        id="11111111-1111-1111-1111-111111111111",
        occurred_at=datetime(2026, 1, 1, tzinfo=UTC),
        actor_subject="some-actor",
        actor_issuer=_ISSUER,
        action="access_role.grant",
        resource_type="principal",
        resource_id="some-target",
        outcome="applied",
        details={"access_role": "SystemAdmin"},
    )
    audit_store = FakeAuditStore(
        query_result=AuditQueryPage(events=(sample_event,), next_cursor="opaque-cursor")
    )
    filters = AuditQueryFilters(action="access_role.grant")

    result = list_audit_events(
        _FIRST_CALLER,
        filters=filters,
        cursor="prior-cursor",
        page_size=10,
        access_role_store=access_role_store,
        audit_store=audit_store,
    )

    assert result == audit_store.query_result
    assert audit_store.query_calls == [(filters, "prior-cursor", 10)]


def test_list_audit_events_never_falls_open_when_the_audit_store_is_unreachable() -> None:
    """AC-BI-011 (query half): the audit store failing to connect surfaces as the same
    `AuthorizationStoreUnavailableError` every other role-gated action uses (issue #147).
    """
    access_role_store = FakeAccessRoleStore(expected_owner=_FIRST_CALLER)
    resolve_active_roles(_FIRST_CALLER, store=access_role_store)
    audit_store = FakeAuditStore(
        raise_on_query=AuditPostgresUnavailableError("simulated audit store outage")
    )

    with pytest.raises(AuthorizationStoreUnavailableError) as exc_info:
        list_audit_events(
            _FIRST_CALLER,
            filters=AuditQueryFilters(),
            cursor=None,
            page_size=25,
            access_role_store=access_role_store,
            audit_store=audit_store,
        )

    assert str(exc_info.value) == "The authorization store is temporarily unavailable."


def test_list_audit_events_translates_a_malformed_cursor_into_invalid_query_filter_error() -> None:
    """A malformed `cursor` (decoded by `AuditStore.query` itself) surfaces as the same
    `InvalidAuditQueryFilterError` family every other invalid filter uses, naming 'cursor'.
    """
    access_role_store = FakeAccessRoleStore(expected_owner=_FIRST_CALLER)
    resolve_active_roles(_FIRST_CALLER, store=access_role_store)
    audit_store = FakeAuditStore(raise_on_query=AuditInvalidCursorError("simulated bad cursor"))

    with pytest.raises(InvalidAuditQueryFilterError) as exc_info:
        list_audit_events(
            _FIRST_CALLER,
            filters=AuditQueryFilters(),
            cursor="not-a-real-cursor",
            page_size=25,
            access_role_store=access_role_store,
            audit_store=audit_store,
        )

    assert "cursor" in str(exc_info.value)
