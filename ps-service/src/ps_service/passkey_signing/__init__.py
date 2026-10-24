"""Passkey signing: WebAuthn-signed approval of privileged actions, with its own Postgres stores.

The service acts as its own WebAuthn relying party for transaction signing, separate from the
identity provider's login-time WebAuthn. A privileged action is parked as a pending approval; the
approver signs it with a passkey in a companion browser; only then does the action run. Domain
path: `ps.service.passkeysigning` (`docs/architecture/ps-service-container-architecture.md`).

Public surface re-exported here:
    Row types: `PendingApprovalRow`, `SigningCredentialRow`.
    Stores: `PendingApprovalStore` and `SigningCredentialStore` (the protocols callers depend
        on) and their Postgres implementations `PsycopgPendingApprovalStore` and
        `PsycopgSigningCredentialStore`.
    Connection helpers: `connect_from_config` opens a fresh connection from the service
        configuration; `check_connectivity_from_config` probes it for readiness.
    Errors: `PendingApprovalPersistenceError`, `SigningCredentialPersistenceError`,
        `PasskeySigningPostgresConnectionError`, `MigrationApplyError`.

The HTTP ceremony lives in `ps_service.passkey_signing.router`, the WebAuthn helpers in
`webauthn_rp`, and the executor registries in `executors`; none of those is re-exported here.
"""

from __future__ import annotations

import ps_service.passkey_signing.audit_actions  # noqa: F401  # pyright: ignore[reportUnusedImport] -- registers `passkey.enroll` on package import
from ps_service.passkey_signing.errors import (
    MigrationApplyError,
    PasskeySigningPostgresConnectionError,
    PendingApprovalPersistenceError,
    SigningCredentialPersistenceError,
)
from ps_service.passkey_signing.models import PendingApprovalRow, SigningCredentialRow
from ps_service.passkey_signing.signing_credential_store import (
    PsycopgSigningCredentialStore,
    SigningCredentialStore,
)
from ps_service.passkey_signing.store import (
    PendingApprovalStore,
    PsycopgPendingApprovalStore,
    check_connectivity_from_config,
    connect_for_migrations,
    connect_from_config,
)

__all__ = [
    "MigrationApplyError",
    "PasskeySigningPostgresConnectionError",
    "PendingApprovalPersistenceError",
    "PendingApprovalRow",
    "PendingApprovalStore",
    "PsycopgPendingApprovalStore",
    "PsycopgSigningCredentialStore",
    "SigningCredentialPersistenceError",
    "SigningCredentialRow",
    "SigningCredentialStore",
    "check_connectivity_from_config",
    "connect_for_migrations",
    "connect_from_config",
]
