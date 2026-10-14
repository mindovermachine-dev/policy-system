"""Domain-specific exception types for `ps_service.authz`.

One exception type per distinct failure boundary this component owns, never a generic
`Exception` or `ValueError`. Mirrors the shape of `ps_service.passkey_signing.errors`.
"""

from __future__ import annotations


class AccessRoleAssignmentPersistenceError(Exception):
    """A read or write against the access-role assignment or audit store failed.

    Raised by `ps_service.authz.store.PsycopgAccessRoleStore` when the underlying `psycopg`
    call raises (or a count query unexpectedly yields no row). The driver-level
    `psycopg.Error` is chained as `__cause__`, so no raw driver exception crosses this
    component's boundary. `ps_service.authz.service` translates it into
    `AuthorizationStoreUnavailableError`, so callers of the service functions see the
    fail-closed error rather than this one.
    """


class AccessRoleSystemOwnerFloorRaceError(Exception):
    """A concurrent revoke would drop the active `SystemOwner` count to zero.

    Raised by `ps_service.authz.store.PsycopgAccessRoleStore.revoke` for a `SYSTEM_OWNER`
    revoke when the recount taken under the advisory lock, immediately before the delete,
    still shows exactly one active `SystemOwner`. That happens when two revokes pass the
    pre-mutation `rules.enforce_system_owner_floor` check against the same stale count.
    Nothing is deleted or audited when it is raised. This is the atomic second layer; the
    pre-mutation rule check in `ps_service.authz.service.revoke_role` is the first.
    `revoke_role` catches it, records a rejected-revoke audit event and re-raises it as the
    API-boundary `SystemOwnerFloorViolationError`, so service callers never see this type.
    """


class AccessRoleBootstrapConfigurationError(Exception):
    """The bootstrap-owner identity is not configured and the local-test bypass is inactive.

    Raised by `ps_service.authz.startup.require_bootstrap_owner_configured` at application
    construction when `PS_AUTHZ_BOOTSTRAP_OWNER_SUBJECT` or `PS_AUTHZ_BOOTSTRAP_OWNER_ISSUER`
    is unset. It is a process-configuration failure of the same category as
    `ps_service.auth.errors.AuthConfigurationError`, owned by this component. The message
    names exactly which variable or variables are unset and names
    `PS_SERVICE_LOCAL_TEST_BYPASS` as the local-only alternative. The caller should fix the
    environment and restart; the process must not serve requests.
    """
