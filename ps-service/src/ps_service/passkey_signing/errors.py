"""Exceptions raised by the passkey-signing component.

One type per failure boundary the component owns, so callers never see a raw driver exception
cross its boundary and never need to catch a bare `Exception`. Each type's docstring says when it
is raised and what the caller can do.
"""

from __future__ import annotations


class PendingApprovalPersistenceError(Exception):
    """A read or write against the `pending_approvals` Postgres table failed.

    Raised by `PsycopgPendingApprovalStore` when the underlying `psycopg` call raises, or when
    an insert unexpectedly returns no row. The driver error is chained as `__cause__`. The
    caller cannot recover the operation; the approval request fails and may be retried once
    Postgres is healthy.
    """


class PasskeySigningPostgresConnectionError(Exception):
    """The passkey-signing Postgres instance is configured but unreachable.

    Raised by `check_connectivity_from_config` (the startup and readiness probe) when opening a
    connection or the `SELECT 1` round trip fails. The message names the host and port; the
    driver error is chained as `__cause__`. The operator should check that Postgres is running
    and reachable with the configured credentials.
    """


class MigrationApplyError(Exception):
    """A `.sql` migration file under `passkey_signing/migrations/` failed to apply.

    Raised by `apply_pending_migrations`. The failing file's name is in the message, and the
    underlying `psycopg.Error` is chained as `__cause__`. The file's transaction is rolled
    back, so the operator can fix the migration and run it again.
    """


class SigningCredentialPersistenceError(Exception):
    """A read or write against the `signing_credentials` Postgres table failed.

    Raised by `PsycopgSigningCredentialStore` when the underlying `psycopg` call raises, or when
    an insert unexpectedly returns no row. The driver error is chained as `__cause__`. The
    caller cannot recover the operation; it may be retried once Postgres is healthy.
    """
