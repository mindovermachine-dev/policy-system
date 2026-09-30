"""Domain-specific exception types for `ps_service.passkey_signing` (issue #131).

One exception type per distinct failure boundary this component owns, never
a generic `Exception`/`ValueError` (L1/L2 Error Handling) -- mirrors the
shape of every other `ps_service` component's `errors.py` module (e.g.
`ps_service.curated_source.errors`, `ps_service.query_engine.errors`).
"""

from __future__ import annotations


class PendingApprovalPersistenceError(Exception):
    """A read or write against the `pending_approvals` Postgres store failed.

    Raised by :class:`ps_service.passkey_signing.store.PsycopgPendingApprovalStore`
    when the underlying `psycopg` call raises -- wraps the driver-level
    `psycopg.Error` into a domain-specific type, mirroring
    `RuntimeConfigPersistenceError`'s/`CompanyMergePersistenceError`'s
    own "wrap the driver exception" convention. Callers never see a raw
    `psycopg` exception cross this component's boundary.
    """


class PasskeySigningPostgresConnectionError(Exception):
    """The Passkey Signing Postgres instance is unreachable.

    Raised by :func:`ps_service.passkey_signing.store.check_connectivity_from_config`
    (the startup/dependency-health probe) when `connect_from_config` or a
    minimal round-trip query fails -- mirrors `ps_service.ingestion.
    falkordb_client.FalkorDBConnectionError`'s exact shape.
    """


class MigrationApplyError(Exception):
    """A `.sql` migration file under `passkey_signing/migrations/` failed to apply.

    Raised by :func:`ps_service.passkey_signing.migration_runner.apply_pending_migrations`;
    wraps the underlying `psycopg.Error`. The failing migration's filename is
    named in the message so an operator can find and fix it directly.
    """


class SigningCredentialPersistenceError(Exception):
    """A read or write against the `signing_credentials` Postgres store failed.

    Raised by :class:`ps_service.passkey_signing.signing_credential_store.
    PsycopgSigningCredentialStore` when the underlying `psycopg` call raises --
    mirrors `PendingApprovalPersistenceError`'s own "wrap the driver
    exception" convention exactly (issue #131 Slice 2).
    """
