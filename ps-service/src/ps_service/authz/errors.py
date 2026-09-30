"""Domain-specific exception types for `ps_service.authz` (issue #133).

One exception type per distinct failure boundary this component owns, never
a generic `Exception`/`ValueError` (L1/L2 Error Handling) -- mirrors the
shape of `ps_service.passkey_signing.errors` exactly.
"""

from __future__ import annotations


class AccessRoleAssignmentPersistenceError(Exception):
    """A read or write against the authz assignment/audit store failed (issue #133).

    Raised by :class:`ps_service.authz.store.PsycopgAccessRoleStore` when the
    underlying `psycopg` call raises -- wraps the driver-level `psycopg.Error`
    into a domain-specific type, mirroring
    `PendingApprovalPersistenceError`'s own "wrap the driver exception"
    convention. Callers never see a raw `psycopg` exception cross this
    component's boundary.
    """


class AccessRoleSystemOwnerFloorRaceError(Exception):
    """A concurrent revoke race would drop active `SystemOwner`s to zero (PLAN.md §0.10).

    Raised by :meth:`ps_service.authz.store.PsycopgAccessRoleStore.revoke`'s
    `SYSTEM_OWNER` branch when, despite `ps_service.authz.service.revoke_role`'s
    own pre-mutation `rules.enforce_system_owner_floor` check already having
    passed against a possibly-stale count, the advisory-locked recount taken
    immediately before the delete still shows exactly one active
    `SystemOwner` remaining -- a genuine concurrent-revoke race between two
    callers' floor checks (defensive, second-layer; `service.py`'s own
    pre-mutation check is the primary guard). Caught by `revoke_role` and
    re-raised as the API-boundary `SystemOwnerFloorViolationError`, mirroring
    how `ps_service.persistence.StatePostgresConnectionError`/
    `AccessRoleAssignmentPersistenceError` are translated there.
    """


class AccessRoleBootstrapConfigurationError(Exception):
    """`PS_AUTHZ_BOOTSTRAP_OWNER_SUBJECT`/`_ISSUER` are missing and the bypass is inactive.

    Raised by :func:`ps_service.authz.startup.require_bootstrap_owner_configured`
    (issue #144, AC-BI-001) -- a distinct process-configuration boundary, same
    category as `ps_service.auth.errors.AuthConfigurationError` but owned by
    this component, not `ps_service.auth` (this module's own docstring: one
    exception type per distinct failure boundary). The message names exactly
    which variable(s) are unset and `PS_SERVICE_LOCAL_TEST_BYPASS` as the
    local-only alternative -- never a generic "authz misconfigured" message.
    """
