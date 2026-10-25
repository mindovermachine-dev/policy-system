"""Operator command that brings `ps_state` from any state to ready.

Run as `python -m ps_service.graph_gateway.provision` with ADMIN credentials in the
environment. In one idempotent run, on one admin connection, it (1) applies the pending ordinary
component migrations (`ps_service.state_migrations`) as the `ps_state` application role -- via
`SET LOCAL ROLE`, so `ps_state` owns every public table exactly as if the service had applied
them itself -- and then (2) applies the `graph_gateway` migration through
`ps_service.persistence.privileged_migration_runner`, so the immutable `graph_log` tables are
owned by a dedicated non-login owner role and the `ps_state` application role only holds INSERT
and SELECT on them (plus UPDATE on the applied-marker table), and finally (3) applies the
`audit_integrity` migration the same way, which moves `audit_events` into the owner-protected
`audit_log` schema. From an empty database it needs no other step; a repeat run applies
nothing. All runners take the migration advisory lock, so it may run while a service replica
starts. The service itself never holds the admin credential.

Environment (names only; values are never printed):

* `PS_STATE_POSTGRES_HOST`, `PS_STATE_POSTGRES_DATABASE`, `PS_STATE_POSTGRES_USER` -- the target
  server, database and application role (required); `PS_STATE_POSTGRES_PORT` (default 5432).
* `PS_STATE_ADMIN_POSTGRES_USER`, `PS_STATE_ADMIN_POSTGRES_PASSWORD` -- the admin credential
  (required).
* `PS_STATE_GRAPH_OWNER_ROLE` -- the owner role name (default `ps_state_graph_owner`).
* `PS_STATE_PROVISION_LOCK_TIMEOUT_SECONDS` -- how long to wait for the migration lock another
  runner (a starting service replica) holds before failing (default 60).
* `PS_STATE_PROVISION_CONNECT_TIMEOUT_SECONDS` -- how long to keep retrying while the server
  does not accept connections yet (default 120). Only "no server answered" and "server starting
  up" are retried; an authentication or permission failure exits immediately.

A failing ordinary migration exits 1 naming it (component, file, exception class) before any
`graph_log` or `audit_log` object is created; progress already committed stays and the next run
resumes.
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
from psycopg import pq
from psycopg.conninfo import make_conninfo

from ps_service.logging.errors import LoggingLifecycleError
from ps_service.logging.facade import emit_log_entry
from ps_service.persistence import (
    MigrationSource,
    StatePostgresMigrationApplyError,
    StatePostgresMigrationLockError,
    StatePostgresProvisioningError,
    apply_pending_migrations,
)
from ps_service.persistence.migration_runner import discover_migration_files
from ps_service.persistence.privileged_migration_runner import apply_privileged_migrations
from ps_service.state_migrations import (
    AUDIT_INTEGRITY_MIGRATION_SOURCE,
    GRAPH_GATEWAY_MIGRATION_SOURCE,
    ORDINARY_STATE_MIGRATION_SOURCES,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from psycopg.rows import TupleRow

    from ps_service.logging.emitter import LogEmitter

_DEFAULT_PORT = 5432
_DEFAULT_OWNER_ROLE = "ps_state_graph_owner"
_COMPONENT = "graph_gateway"
_CONNECT_TIMEOUT_SECONDS = 30
_DEFAULT_LOCK_DEADLINE_SECONDS = 60
_DEFAULT_CONNECT_DEADLINE_SECONDS = 120
_CONNECT_RETRY_INTERVAL_SECONDS = 1.0
_PARSER_DESCRIPTION = (
    "Bring the ps_state database from any state to ready: apply the pending ordinary component "
    "migrations, the graph_gateway migration and the audit_integrity migration in one idempotent "
    "admin run. "
    "Reads its target and admin credential from the environment: "
    "PS_STATE_POSTGRES_HOST/PORT/DATABASE/USER, PS_STATE_ADMIN_POSTGRES_USER, "
    "PS_STATE_ADMIN_POSTGRES_PASSWORD and PS_STATE_GRAPH_OWNER_ROLE."
)


@dataclass(frozen=True)
class ProvisioningTarget:
    """Where and as whom to provision: the target database, the app role, the admin credential.

    Attributes:
        host: Postgres server host name.
        port: Postgres server TCP port.
        database: Name of the `ps_state` database.
        app_user: The `ps_state` application role that owns the ordinary tables.
        admin_user: Admin login used for the whole run.
        admin_password: Admin password; excluded from `repr`.
        owner_role: Non-login role that owns the immutable `graph_log` tables.
        connect_timeout_seconds: How long to keep retrying while the server is not ready.
        lock_timeout_seconds: How long to wait for the migration advisory lock; at least 1.
    """

    host: str
    port: int
    database: str
    app_user: str
    admin_user: str
    admin_password: str = field(repr=False)
    owner_role: str
    connect_timeout_seconds: int = _DEFAULT_CONNECT_DEADLINE_SECONDS
    lock_timeout_seconds: int = _DEFAULT_LOCK_DEADLINE_SECONDS

    @classmethod
    def from_environ(cls, environ: Mapping[str, str]) -> ProvisioningTarget:
        """Read the target from `environ`, naming (never echoing) any missing variable.

        Args:
            environ: Environment variable names to values (see the module docstring for the
                names, defaults and which are required).

        Returns:
            The validated target; optional values not set take their defaults.

        Raises:
            StatePostgresProvisioningError: a required variable is unset or blank, a port or
                timeout is not an integer, or the lock timeout is below 1.
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
        raw_lock_deadline = environ.get(
            "PS_STATE_PROVISION_LOCK_TIMEOUT_SECONDS", str(_DEFAULT_LOCK_DEADLINE_SECONDS)
        ).strip()
        if not raw_lock_deadline.isdecimal() or int(raw_lock_deadline) < 1:
            message = "PS_STATE_PROVISION_LOCK_TIMEOUT_SECONDS must be a positive integer"
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
            lock_timeout_seconds=int(raw_lock_deadline),
        )


@dataclass(frozen=True)
class ProvisionResult:
    """What one provisioning run applied: `component/file` ordinary entries, bare file names.

    Attributes:
        ordinary_applied: `component/file` of each ordinary migration applied, in order; empty
            when none was pending.
        graph_gateway_applied: File names of the `graph_gateway` migrations applied, in order;
            empty when none was pending.
        audit_integrity_applied: File names of the `audit_integrity` migrations applied, in
            order; empty when none was pending.
    """

    ordinary_applied: list[str]
    graph_gateway_applied: list[str]
    audit_integrity_applied: list[str] = field(default_factory=list)


def _server_is_up(target: ProvisioningTarget) -> bool:
    """Whether a server answered at `target` and is past startup, so a failed login is a refusal.

    A failed `psycopg.connect` cannot tell this apart: libpq keeps the SQLSTATE of an error the
    server sends during connection startup to itself, so every connection-time failure arrives
    with `sqlstate=None`, whether the server is absent, starting up or refusing the credential.
    `PQping` (the check `pg_isready` makes) classifies it from that SQLSTATE without logging in:
    `PQPING_NO_RESPONSE` means no server answered and `PQPING_REJECT` means the server is up but
    not accepting connections yet (starting up, shutting down, in recovery). Any other answer,
    including an authentication or permission refusal, means the server is up. No password is
    sent; the ping stops before authentication.
    """
    conninfo = make_conninfo(
        host=target.host,
        port=target.port,
        dbname=target.database,
        user=target.admin_user,
        connect_timeout=_CONNECT_TIMEOUT_SECONDS,
    )
    return pq.PGconn.ping(conninfo.encode()) not in {pq.Ping.NO_RESPONSE, pq.Ping.REJECT}


def _connect_when_server_is_ready(target: ProvisioningTarget) -> psycopg.Connection[TupleRow]:
    """Connect as the admin, retrying until the server accepts connections or the deadline.

    The connection is attempted only once the server is up; a connection failure while it is up
    is a refusal and raised at once. If the server went away again in between, the wait goes on.

    Args:
        target: The target whose host, database, admin credential and connect timeout are used.

    Returns:
        An open admin connection; the caller closes it.

    Raises:
        StatePostgresProvisioningError: the server did not accept connections in time (fixed
            message, no host or driver text).
        psycopg.OperationalError: the server answered and refused (authentication, permission);
            retrying would not help.
    """
    deadline = time.monotonic() + target.connect_timeout_seconds
    while True:
        if _server_is_up(target):
            try:
                return psycopg.connect(
                    host=target.host,
                    port=target.port,
                    dbname=target.database,
                    user=target.admin_user,
                    password=target.admin_password,
                    connect_timeout=_CONNECT_TIMEOUT_SECONDS,
                )
            except psycopg.OperationalError:
                if _server_is_up(target):
                    raise
        if time.monotonic() >= deadline:
            message = (
                "ps_state Postgres did not accept connections within "
                f"{target.connect_timeout_seconds} seconds"
            )
            raise StatePostgresProvisioningError(message)
        time.sleep(_CONNECT_RETRY_INTERVAL_SECONDS)


def _qualify(sources: Sequence[MigrationSource], applied: Sequence[str]) -> list[str]:
    """Return `component/file` for each applied filename (the runner applies in source order)."""
    remaining = iter(applied)
    wanted = next(remaining, None)
    qualified: list[str] = []
    for source in sources:
        for migration_file in discover_migration_files(source.directory):
            if migration_file.name == wanted:
                qualified.append(f"{source.component}/{wanted}")
                wanted = next(remaining, None)
    return qualified


def provision(target: ProvisioningTarget, *, emitter: LogEmitter | None = None) -> ProvisionResult:
    """Bring `ps_state` from any state to ready with the admin credential, in one idempotent run.

    On one admin connection: apply the pending ordinary component migrations as the application
    role (`run_as_role`, so it owns every public object exactly as service startup would have
    made it), then the owner-protected `graph_gateway` migration, then the owner-protected
    `audit_integrity` migration. The order is load-bearing: `graph_gateway`'s first (applied,
    immutable) migration references `public.audit_events` by that name, so it must run while the
    table is still there, and `audit_integrity` then moves the table (the reference follows it).
    An ordinary failure raises before any `graph_log` or `audit_log` object is created; partial
    ordinary progress stays committed and the next run resumes.

    Args:
        target: Where and as whom to provision.
        emitter: Destination of the migration log entries; `None` uses the process-wide one.

    Returns:
        The migrations applied by this run; every list is empty when the database was already up
        to date.

    Raises:
        StatePostgresProvisioningError: The server did not accept connections in time, or a
            provisioning precondition failed.
        StatePostgresMigrationApplyError: A migration failed to apply.
        StatePostgresMigrationLockError: The migration lock was not obtained in time.
        psycopg.OperationalError: The server refused the connection (authentication, permission).

    Side Effects:
        Applies migrations to the target database, creates the owner-protected `graph_log`
        tables and moves `audit_events` into the owner-protected `audit_log` schema.
    """
    with _connect_when_server_is_ready(target) as conn:
        ordinary = apply_pending_migrations(
            conn,
            sources=ORDINARY_STATE_MIGRATION_SOURCES,
            run_as_role=target.app_user,
            emitter=emitter,
            lock_timeout_seconds=target.lock_timeout_seconds,
        )
        graph_gateway = apply_privileged_migrations(
            conn,
            sources=[GRAPH_GATEWAY_MIGRATION_SOURCE],
            owner_role=target.owner_role,
            app_role=target.app_user,
            emitter=emitter,
            lock_timeout_seconds=target.lock_timeout_seconds,
        )
        audit_integrity = apply_privileged_migrations(
            conn,
            sources=[AUDIT_INTEGRITY_MIGRATION_SOURCE],
            owner_role=target.owner_role,
            app_role=target.app_user,
            emitter=emitter,
            lock_timeout_seconds=target.lock_timeout_seconds,
        )
    return ProvisionResult(
        ordinary_applied=_qualify(ORDINARY_STATE_MIGRATION_SOURCES, ordinary),
        graph_gateway_applied=graph_gateway,
        audit_integrity_applied=audit_integrity,
    )


def _emit_provision_entry(*, outcome: str, extra: Mapping[str, object]) -> None:
    """Log one `provision_graph_log` entry (role names, filenames, counts; never credentials)."""
    # A bare process has no configured default emitter; the entry is diagnostics only.
    with contextlib.suppress(LoggingLifecycleError):
        emit_log_entry(
            component=_COMPONENT, action="provision_graph_log", outcome=outcome, extra=extra
        )


def _build_parser() -> argparse.ArgumentParser:
    """Build the argument parser, whose help text is `_PARSER_DESCRIPTION`."""
    return argparse.ArgumentParser(
        prog="python -m ps_service.graph_gateway.provision",
        description=_PARSER_DESCRIPTION,
    )


def main(argv: Sequence[str] | None = None, environ: Mapping[str, str] | None = None) -> int:
    """Provision the graph log tables; return 0 on success and 1 on any failure.

    Failures print one sanitized line to stderr (variable names, role names, migration filenames
    only -- never a host, SQL, a driver message or the admin password).

    Args:
        argv: Command-line arguments; `None` reads `sys.argv`. The command takes none besides
            `--help`.
        environ: Environment to read the target from; `None` uses `os.environ`.

    Returns:
        0 when provisioning succeeded (including a run that applied nothing), 1 on any failure.

    Side Effects:
        Provisions the database (see `provision`), writes one line to stdout on success or to
        stderr on failure, and emits a `provision_graph_log` log entry.
    """
    _build_parser().parse_args(argv)
    try:
        target = ProvisioningTarget.from_environ(os.environ if environ is None else environ)
        result = provision(target)
    except (StatePostgresProvisioningError, StatePostgresMigrationApplyError) as exc:
        _emit_provision_entry(outcome="failure", extra={"reason": type(exc).__name__})
        sys.stderr.write(f"graph_gateway provisioning failed: {_sanitized(exc)}\n")
        return 1
    except psycopg.Error as exc:
        _emit_provision_entry(outcome="failure", extra={"reason": type(exc).__name__})
        sys.stderr.write(f"graph_gateway provisioning failed: {type(exc).__name__}\n")
        return 1
    applied = [
        *result.ordinary_applied,
        *result.graph_gateway_applied,
        *result.audit_integrity_applied,
    ]
    _emit_provision_entry(
        outcome="success",
        extra={
            "ordinary_applied": result.ordinary_applied,
            "applied": result.graph_gateway_applied,
            "audit_integrity_applied": result.audit_integrity_applied,
            "owner_role": target.owner_role,
        },
    )
    sys.stdout.write(
        f"provisioning: applied {len(result.ordinary_applied)} ordinary + "
        f"{len(result.graph_gateway_applied)} graph_gateway + "
        f"{len(result.audit_integrity_applied)} audit_integrity migration(s): "
        f"{', '.join(applied) or 'none (already up to date)'}\n"
    )
    return 0


def _sanitized(exc: StatePostgresProvisioningError | StatePostgresMigrationApplyError) -> str:
    """Return failure text safe to print.

    A provisioning or lock error carries a fixed, already-sanitized message; a failed migration
    prints its name plus the exception class of the cause, never the driver text.
    """
    if isinstance(exc, (StatePostgresProvisioningError, StatePostgresMigrationLockError)):
        return str(exc)
    migration = str(exc).split(" failed to apply", maxsplit=1)[0]
    return f"{migration} failed to apply ({type(exc.__cause__).__name__})"


if __name__ == "__main__":
    sys.exit(main())
