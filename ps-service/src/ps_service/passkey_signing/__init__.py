"""ps_service.passkey_signing -- package front door (issue #131).

PS Service's own, independent WebAuthn relying party for transaction-signing
(distinct from Authentik's login-time WebAuthn, PLAN.md §0.3), plus its own
PostgreSQL-backed store for pending-approval records and enrolled signing
credentials. Domain path: `ps.service.passkeysigning`
(`docs/architecture/ps-service-container-architecture.md`).

This slice ships the data layer only: `PendingApprovalRow`/
`PendingApprovalStore`/`PsycopgPendingApprovalStore` and the hand-rolled
migration runner. The WebAuthn relying party and the MCP/REST tool wiring
land in later slices (PLAN.md §4).

Re-exports the store front door and its domain-specific errors, matching the
`ps_service.curated_source`/`ps_service.restore` package front doors' own
re-export convention.
"""

from __future__ import annotations

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
    "connect_from_config",
]
