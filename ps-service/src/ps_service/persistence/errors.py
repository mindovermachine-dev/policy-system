"""Domain-specific exception types for `ps_service.persistence` (issue #130).

One exception type per distinct failure boundary this component owns, never
a generic `Exception`/`ValueError` (L1/L2 Error Handling).
"""

from __future__ import annotations

from typing import Literal

MissingMigrationReason = Literal[
    "migration_not_recorded", "table_missing", "state_role_controls_table"
]
"""Why the privileged migration check failed (a fixed vocabulary, safe to log)."""


class StatePostgresConnectionError(Exception):
    """The PS state Postgres instance is unreachable, or unconfigured (PLAN.md §0.11).

    Raised by :func:`ps_service.persistence.connection.connect_from_config`
    immediately when `config.state_postgres_host` is `None` (never a doomed
    `psycopg.connect(host=None, ...)` attempt) and by
    :func:`ps_service.persistence.connection.check_connectivity_from_config`
    when the instance is configured but unreachable. Unlike
    `ps_service.passkey_signing.store.check_connectivity_from_config`'s own
    "no-op when unset" contract, every state-store caller must fail closed --
    an unconfigured store means every role-gated action is permanently
    rejected for everyone, the correct, conservative, deny-by-default outcome
    (AC-BI-011).
    """


class StatePostgresMigrationApplyError(Exception):
    """A `.sql` migration file failed to apply against the PS state Postgres.

    Raised by :func:`ps_service.persistence.migration_runner.apply_pending_migrations`;
    wraps the underlying `psycopg.Error`. The failing migration's component and
    filename are named in the message so an operator can find and fix it
    directly.
    """


class StatePostgresProvisioningError(Exception):
    """The privileged provisioning path cannot proceed, or its inputs are unusable (issue #205).

    Raised by :mod:`ps_service.persistence.privileged_migration_runner` and
    `ps_service.graph_gateway.provision` for a precondition that would defeat the
    owner-role protection (the app role is a member of the owner role, the owner
    role can log in, the tracking table is not owned by the app role), for an unknown
    role token in a migration file, and for a missing provisioning environment
    variable. Messages name variables, roles and migration files only, never a
    host, a password or driver text.
    """


class GraphLogMigrationMissingError(StatePostgresProvisioningError):
    """The privileged `graph_gateway` migration is not in place, so startup must fail closed (#205).

    Raised by
    :func:`ps_service.persistence.privileged_migration_runner.verify_privileged_migrations_applied`
    when the migration is not recorded, a table it creates is missing, or the application role
    owns (or is a member of the owner of) an immutable table. The message is fixed text naming
    the migration as `<component>/<filename>` and the remedy; it never carries a host, SQL or
    driver text. `missing_migration` and `reason` are the same two facts for structured logging.
    """

    def __init__(self, missing_migration: str, *, reason: MissingMigrationReason) -> None:
        """Build the fixed message for `missing_migration` (`<component>/<filename>`)."""
        super().__init__(
            f"privileged migration {missing_migration} is not in place ({reason}); run "
            "`python -m ps_service.graph_gateway.provision` with the admin credential"
        )
        self.missing_migration = missing_migration
        self.reason: MissingMigrationReason = reason
