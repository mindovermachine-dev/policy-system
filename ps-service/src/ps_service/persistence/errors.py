"""Domain-specific exception types for `ps_service.persistence` (issue #130).

One exception type per distinct failure boundary this component owns, never
a generic `Exception`/`ValueError` (L1/L2 Error Handling).
"""

from __future__ import annotations


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
