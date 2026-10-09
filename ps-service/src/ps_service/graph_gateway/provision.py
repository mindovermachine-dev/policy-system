"""Operator command that creates and migrates the owner-protected graph log tables (issue #205).

Run as `python -m ps_service.graph_gateway.provision` with ADMIN credentials in the
environment. It applies the `graph_gateway` migrations through
`ps_service.persistence.privileged_migration_runner`, so the immutable `graph_log` tables are
owned by a dedicated non-login owner role and the `ps_state` application role only holds INSERT
and SELECT on them (plus UPDATE on the applied-marker table). Idempotent: a repeat run applies
nothing. The service itself never holds the admin credential.

Environment (names only; values are never printed):

* `PS_STATE_POSTGRES_HOST`, `PS_STATE_POSTGRES_DATABASE`, `PS_STATE_POSTGRES_USER` -- the target
  server, database and application role (required); `PS_STATE_POSTGRES_PORT` (default 5432).
* `PS_STATE_ADMIN_POSTGRES_USER`, `PS_STATE_ADMIN_POSTGRES_PASSWORD` -- the admin credential
  (required).
* `PS_STATE_GRAPH_OWNER_ROLE` -- the owner role name (default `ps_state_graph_owner`).
* `PS_STATE_PROVISION_CONNECT_TIMEOUT_SECONDS` -- how long to keep retrying while the server
  does not accept connections yet (default 120). Only "no server answered" and "server starting
  up" are retried; an authentication or permission failure exits immediately.

A migration that needs a table the service's own startup migrations create (the log links to
`public.audit_events`) fails with exit 1; the chart's Job retries it until the service has run.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import sys
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import psycopg

from ps_service.graph_gateway import MIGRATIONS_DIR
from ps_service.logging.errors import LoggingLifecycleError
from ps_service.logging.facade import emit_log_entry
from ps_service.persistence import (
    MigrationSource,
    StatePostgresMigrationApplyError,
    StatePostgresProvisioningError,
)
from ps_service.persistence.privileged_migration_runner import apply_privileged_migrations

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from psycopg.rows import TupleRow

    from ps_service.logging.emitter import LogEmitter

_DEFAULT_PORT = 5432
_DEFAULT_OWNER_ROLE = "ps_state_graph_owner"
_COMPONENT = "graph_gateway"
_CONNECT_TIMEOUT_SECONDS = 30
_DEFAULT_CONNECT_DEADLINE_SECONDS = 120
_CONNECT_RETRY_INTERVAL_SECONDS = 1.0
_SQLSTATE_CANNOT_CONNECT_NOW = "57P03"


@dataclass(frozen=True)
class ProvisioningTarget:
    """Where and as whom to provision: the target database, the app role, the admin credential."""

    host: str
    port: int
    database: str
    app_user: str
    admin_user: str
    admin_password: str = field(repr=False)
    owner_role: str
    connect_timeout_seconds: int = _DEFAULT_CONNECT_DEADLINE_SECONDS

    @classmethod
    def from_environ(cls, environ: Mapping[str, str]) -> ProvisioningTarget:
        """Read the target from `environ`, naming (never echoing) any missing variable.

        Raises:
            StatePostgresProvisioningError: a required variable is unset or blank, or the
                port is not an integer.
        """

        def required(name: str) -> str:
            value = environ.get(name, "").strip()
            if not value:
                message = f"{name} is required"
                raise StatePostgresProvisioningError(message)
            return value

        raw_port = environ.get("PS_STATE_POSTGRES_PORT", str(_DEFAULT_PORT)).strip()
        if not raw_port.isdecimal():
            message = "PS_STATE_POSTGRES_PORT must be an integer"
            raise StatePostgresProvisioningError(message)
        raw_deadline = environ.get(
            "PS_STATE_PROVISION_CONNECT_TIMEOUT_SECONDS", str(_DEFAULT_CONNECT_DEADLINE_SECONDS)
        ).strip()
        if not raw_deadline.isdecimal():
            message = "PS_STATE_PROVISION_CONNECT_TIMEOUT_SECONDS must be an integer"
            raise StatePostgresProvisioningError(message)
        return cls(
            host=required("PS_STATE_POSTGRES_HOST"),
            port=int(raw_port),
            database=required("PS_STATE_POSTGRES_DATABASE"),
            app_user=required("PS_STATE_POSTGRES_USER"),
            admin_user=required("PS_STATE_ADMIN_POSTGRES_USER"),
            admin_password=required("PS_STATE_ADMIN_POSTGRES_PASSWORD"),
            owner_role=environ.get("PS_STATE_GRAPH_OWNER_ROLE", "").strip() or _DEFAULT_OWNER_ROLE,
            connect_timeout_seconds=int(raw_deadline),
        )


def _is_server_not_ready(exc: psycopg.OperationalError) -> bool:
    """Whether the failure means "no server answered yet" (no SQLSTATE) or "server starting up"."""
    return exc.sqlstate is None or exc.sqlstate == _SQLSTATE_CANNOT_CONNECT_NOW


def _connect_when_server_is_ready(target: ProvisioningTarget) -> psycopg.Connection[TupleRow]:
    """Connect as the admin, retrying until the server accepts connections or the deadline.

    Raises:
        StatePostgresProvisioningError: the server did not accept connections in time (fixed
            message, no host or driver text).
        psycopg.OperationalError: the server answered and refused (authentication, permission);
            retrying would not help.
    """
    deadline = time.monotonic() + target.connect_timeout_seconds
    while True:
        try:
            return psycopg.connect(
                host=target.host,
                port=target.port,
                dbname=target.database,
                user=target.admin_user,
                password=target.admin_password,
                connect_timeout=_CONNECT_TIMEOUT_SECONDS,
            )
        except psycopg.OperationalError as exc:
            if not _is_server_not_ready(exc):
                raise
            if time.monotonic() >= deadline:
                message = (
                    "ps_state Postgres did not accept connections within "
                    f"{target.connect_timeout_seconds} seconds"
                )
                raise StatePostgresProvisioningError(message) from exc
        time.sleep(_CONNECT_RETRY_INTERVAL_SECONDS)


def provision(target: ProvisioningTarget, *, emitter: LogEmitter | None = None) -> list[str]:
    """Apply the pending `graph_gateway` migrations with the admin credential.

    Returns the migration filenames applied this call (empty when already up to date).
    """
    with _connect_when_server_is_ready(target) as conn:
        return apply_privileged_migrations(
            conn,
            sources=[MigrationSource(_COMPONENT, MIGRATIONS_DIR)],
            owner_role=target.owner_role,
            app_role=target.app_user,
            emitter=emitter,
        )


def _emit_provision_entry(*, outcome: str, extra: Mapping[str, object]) -> None:
    """Log one `provision_graph_log` entry (role names, filenames, counts; never credentials)."""
    # A bare process has no configured default emitter; the entry is diagnostics only.
    with contextlib.suppress(LoggingLifecycleError):
        emit_log_entry(
            component=_COMPONENT, action="provision_graph_log", outcome=outcome, extra=extra
        )


def _build_parser() -> argparse.ArgumentParser:
    return argparse.ArgumentParser(
        prog="python -m ps_service.graph_gateway.provision",
        description=(__doc__ or "").split("\n\n", maxsplit=1)[0]
        + " Reads its target and admin credential from the environment: "
        "PS_STATE_POSTGRES_HOST/PORT/DATABASE/USER, PS_STATE_ADMIN_POSTGRES_USER, "
        "PS_STATE_ADMIN_POSTGRES_PASSWORD and PS_STATE_GRAPH_OWNER_ROLE.",
    )


def main(argv: Sequence[str] | None = None, environ: Mapping[str, str] | None = None) -> int:
    """Provision the graph log tables; return 0 on success and 1 on any failure.

    Failures print one sanitized line to stderr (variable names, role names, migration filenames
    only -- never a host, SQL, a driver message or the admin password).
    """
    _build_parser().parse_args(argv)
    try:
        target = ProvisioningTarget.from_environ(os.environ if environ is None else environ)
        applied = provision(target)
    except (StatePostgresProvisioningError, StatePostgresMigrationApplyError) as exc:
        _emit_provision_entry(outcome="failure", extra={"reason": type(exc).__name__})
        sys.stderr.write(f"graph_gateway provisioning failed: {_sanitized(exc)}\n")
        return 1
    except psycopg.Error as exc:
        _emit_provision_entry(outcome="failure", extra={"reason": type(exc).__name__})
        sys.stderr.write(f"graph_gateway provisioning failed: {type(exc).__name__}\n")
        return 1
    _emit_provision_entry(
        outcome="success", extra={"applied": applied, "owner_role": target.owner_role}
    )
    sys.stdout.write(
        f"graph_gateway provisioning: applied {len(applied)} migration(s): "
        f"{', '.join(applied) or 'none (already up to date)'}\n"
    )
    return 0


def _sanitized(exc: StatePostgresProvisioningError | StatePostgresMigrationApplyError) -> str:
    """Return failure text safe to print: the provisioning error, or migration name plus cause."""
    if isinstance(exc, StatePostgresProvisioningError):
        return str(exc)
    migration = str(exc).split(" failed to apply", maxsplit=1)[0]
    return f"{migration} failed to apply ({type(exc.__cause__).__name__})"


if __name__ == "__main__":
    sys.exit(main())
