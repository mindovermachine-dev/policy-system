"""Privileged SQL migration runner for owner-protected tables (issue #205).

The ordinary runner (`ps_service.persistence.migration_runner`) connects as the `ps_state`
application role, so every table it creates is owned by that role -- and a table owner can
always UPDATE, DELETE, TRUNCATE, ALTER and DROP its own table. Tables that must stay insert-only
therefore need a different owner: this runner connects with ADMIN credentials, makes sure the
non-login owner role exists, applies a component's migration files (statements and tracking row
in one transaction, same `ps_schema_migrations` table and file discovery as the ordinary
runner), and each migration file hands its objects to the owner role with `ALTER ... OWNER TO`.

Migration files name the two roles through the render tokens `@@OWNER_ROLE@@` and
`@@APP_ROLE@@`; the runner substitutes each with a quoted identifier, never a bare string. The
application role is never made a member of the owner role (that would let it `SET ROLE` past
the protection): the runner refuses to proceed if it already is one. The service pod never holds
the admin credential; this runner is driven by the operator-run provisioning command
(`python -m ps_service.graph_gateway.provision`).

Migrations are append-only, exactly as for the ordinary runner.
"""

from __future__ import annotations

import contextlib
import re
from typing import TYPE_CHECKING

import psycopg
from psycopg import sql

from ps_service.logging.errors import LoggingLifecycleError
from ps_service.logging.facade import emit_log_entry
from ps_service.persistence.errors import (
    GraphLogMigrationMissingError,
    StatePostgresMigrationApplyError,
    StatePostgresProvisioningError,
)
from ps_service.persistence.migration_runner import (
    CREATE_TRACKING_TABLE,
    DEFAULT_LOCK_TIMEOUT_SECONDS,
    SELECT_ALREADY_APPLIED,
    acquire_migration_lock,
    apply_migration_file,
    discover_migration_files,
    is_migration_applied,
    set_local_role,
    split_statements,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from psycopg.rows import TupleRow

    from ps_service.logging.emitter import LogEmitter
    from ps_service.persistence.errors import MissingMigrationReason
    from ps_service.persistence.migration_runner import MigrationSource

OWNER_ROLE_TOKEN = "@@OWNER_ROLE@@"  # noqa: S105  # a template token, not a credential
APP_ROLE_TOKEN = "@@APP_ROLE@@"  # noqa: S105  # a template token, not a credential
_ANY_TOKEN = re.compile(r"@@[A-Za-z0-9_]+@@")

_SELECT_ROLE_CAN_LOGIN = "SELECT rolcanlogin FROM pg_roles WHERE rolname = %(role)s"
_SELECT_IS_MEMBER = "SELECT pg_has_role(%(member)s, %(role)s, 'MEMBER')"
_SELECT_TRACKING_TABLE_OWNER = (
    "SELECT pg_get_userbyid(relowner) FROM pg_class WHERE oid = to_regclass('ps_schema_migrations')"
)

_SELECT_TO_REGCLASS = "SELECT to_regclass(%(name)s) IS NOT NULL"
_SELECT_CONTROLS_TABLE = (
    "SELECT pg_has_role(current_user, relowner, 'MEMBER') FROM pg_class "
    "WHERE oid = to_regclass(%(name)s)"
)


def render_migration_sql(sql_text: str, *, owner_role: str, app_role: str) -> str:
    """Substitute the role tokens of a migration file with quoted identifiers.

    Raises:
        StatePostgresProvisioningError: the file contains a `@@NAME@@` token other
            than `@@OWNER_ROLE@@` / `@@APP_ROLE@@` (a typo must fail loudly rather
            than reach the database as literal text).
    """
    replacements = {
        OWNER_ROLE_TOKEN: sql.Identifier(owner_role).as_string(),
        APP_ROLE_TOKEN: sql.Identifier(app_role).as_string(),
    }
    rendered = sql_text
    for token, identifier in replacements.items():
        rendered = rendered.replace(token, identifier)
    unknown = _ANY_TOKEN.search(rendered)
    if unknown is not None:
        message = f"migration file contains unknown role token {unknown.group()}"
        raise StatePostgresProvisioningError(message)
    return rendered


def _ensure_owner_role(
    conn: psycopg.Connection[TupleRow], *, owner_role: str, lock_timeout_seconds: float
) -> None:
    """Create the non-login owner role if absent; refuse an existing role that can log in.

    Takes the migration lock first, so the existence check and the create are one unit that a
    concurrent runner cannot interleave with.
    """
    acquire_migration_lock(conn, timeout_seconds=lock_timeout_seconds, subject="owner role setup")
    with conn.cursor() as cur:
        cur.execute(_SELECT_ROLE_CAN_LOGIN, {"role": owner_role})
        row = cur.fetchone()
        if row is None:
            cur.execute(
                sql.SQL("CREATE ROLE {} NOLOGIN NOINHERIT").format(sql.Identifier(owner_role))
            )
        elif row[0]:
            message = f"role {owner_role} must be NOLOGIN; refusing to use a role that can log in"
            raise StatePostgresProvisioningError(message)
    conn.commit()


def _refuse_app_role_membership(
    conn: psycopg.Connection[TupleRow], *, owner_role: str, app_role: str
) -> None:
    """Fail unless the app role is distinct from, and not a member of, the owner role."""
    with conn.cursor() as cur:
        cur.execute(_SELECT_IS_MEMBER, {"member": app_role, "role": owner_role})
        row = cur.fetchone()
    if app_role == owner_role or (row is not None and row[0]):
        message = (
            f"role {app_role} is, or is a member of, the owner role {owner_role}; "
            "it could SET ROLE past the immutability protection"
        )
        raise StatePostgresProvisioningError(message)


def _ensure_tracking_table_owned_by_app_role(
    conn: psycopg.Connection[TupleRow], *, app_role: str, lock_timeout_seconds: float
) -> None:
    """Create `ps_schema_migrations` as the app role (if absent) and require that it owns it.

    The ordinary runner (running as the app role) must stay able to write to the table, so an
    admin-created tracking table would break the next service start. Runs under the migration
    lock, taken before the role switch.
    """
    acquire_migration_lock(
        conn, timeout_seconds=lock_timeout_seconds, subject="migration tracking table setup"
    )
    set_local_role(conn, app_role)
    with conn.cursor() as cur:
        cur.execute(CREATE_TRACKING_TABLE)
        cur.execute("RESET ROLE")
        cur.execute(_SELECT_TRACKING_TABLE_OWNER)
        row = cur.fetchone()
    conn.commit()
    if row is None or row[0] != app_role:
        message = f"ps_schema_migrations must be owned by {app_role}"
        raise StatePostgresProvisioningError(message)


def _emit_apply_entry(
    *,
    outcome: str,
    applied: Sequence[str],
    already_applied: int,
    owner_role: str,
    emitter: LogEmitter | None,
) -> None:
    """Log one entry per runner call: filenames and role names only, never SQL or credentials."""
    with contextlib.suppress(LoggingLifecycleError):
        emit_log_entry(
            component="persistence",
            action="apply_privileged_migrations",
            outcome=outcome,
            extra={
                "applied": list(applied),
                "already_applied": already_applied,
                "owner_role": owner_role,
            },
            emitter=emitter,
        )


def apply_privileged_migrations(
    admin_conn: psycopg.Connection[TupleRow],
    *,
    sources: Sequence[MigrationSource],
    owner_role: str,
    app_role: str,
    emitter: LogEmitter | None = None,
    lock_timeout_seconds: float = DEFAULT_LOCK_TIMEOUT_SECONDS,
) -> list[str]:
    """Apply every not-yet-recorded migration file of every source, as the admin connection.

    Ensures the NOLOGIN `owner_role` exists, refuses if `app_role` is a member of it, makes sure
    `ps_schema_migrations` exists and is owned by `app_role`, then applies each pending file:
    role tokens rendered, statements and the tracking row committed in one transaction. Returns
    the filenames applied this call (empty on a repeat run). Emits one
    `persistence`/`apply_privileged_migrations` log entry per call naming `<component>/<file>`
    entries and role names.

    The role and tracking-table setup and each pending file run under the same migration
    advisory lock as the ordinary runner (`MIGRATION_LOCK_KEY`), with the tracking table
    re-checked inside it, so a service starting at the same time cannot interleave; the wait is
    bounded by `lock_timeout_seconds`.

    Raises:
        StatePostgresProvisioningError: a precondition failed (see the helpers above).
        StatePostgresMigrationLockError: the migration lock was not granted in time.
        StatePostgresMigrationApplyError: a file's SQL failed; names the component and file.
    """
    applied: list[str] = []
    already_applied_count = 0
    try:
        _ensure_owner_role(
            admin_conn, owner_role=owner_role, lock_timeout_seconds=lock_timeout_seconds
        )
        _refuse_app_role_membership(admin_conn, owner_role=owner_role, app_role=app_role)
        _ensure_tracking_table_owned_by_app_role(
            admin_conn, app_role=app_role, lock_timeout_seconds=lock_timeout_seconds
        )
        for source in sources:
            for migration_file in discover_migration_files(source.directory):
                key = {"component": source.component, "filename": migration_file.name}
                if not is_migration_applied(admin_conn, key) and apply_migration_file(
                    admin_conn,
                    key=key,
                    statements=split_statements(
                        render_migration_sql(
                            migration_file.read_text(encoding="utf-8"),
                            owner_role=owner_role,
                            app_role=app_role,
                        )
                    ),
                    lock_timeout_seconds=lock_timeout_seconds,
                ):
                    applied.append(f"{source.component}/{migration_file.name}")
                else:
                    already_applied_count += 1
    except StatePostgresMigrationApplyError:
        _emit_apply_entry(
            outcome="failure",
            applied=applied,
            already_applied=already_applied_count,
            owner_role=owner_role,
            emitter=emitter,
        )
        raise
    _emit_apply_entry(
        outcome="success",
        applied=applied,
        already_applied=already_applied_count,
        owner_role=owner_role,
        emitter=emitter,
    )
    return [name.split("/", 1)[1] for name in applied]


def _first_migration_name(sources: Sequence[MigrationSource]) -> str:
    """Return `<component>/<filename>` of the first migration file across `sources`."""
    for source in sources:
        for migration_file in discover_migration_files(source.directory):
            return f"{source.component}/{migration_file.name}"
    message = "no privileged migration files to verify"
    raise StatePostgresProvisioningError(message)


def _require_migrations_recorded(
    conn: psycopg.Connection[TupleRow], *, sources: Sequence[MigrationSource]
) -> None:
    """Raise unless every file of every source has a tracking row (read-only)."""
    with conn.cursor() as cur:
        cur.execute(_SELECT_TO_REGCLASS, {"name": "ps_schema_migrations"})
        tracking_table = cur.fetchone()
        has_tracking_table = tracking_table is not None and bool(tracking_table[0])
        for source in sources:
            for migration_file in discover_migration_files(source.directory):
                key = {"component": source.component, "filename": migration_file.name}
                if not has_tracking_table or _is_not_recorded(cur, key):
                    raise GraphLogMigrationMissingError(
                        f"{source.component}/{migration_file.name}",
                        reason="migration_not_recorded",
                    )


def _is_not_recorded(cur: psycopg.Cursor[TupleRow], key: dict[str, str]) -> bool:
    """Whether the tracking table has no row for `key` (component and filename)."""
    cur.execute(SELECT_ALREADY_APPLIED, key)
    return cur.fetchone() is None


def _require_tables_in_protected_state(
    conn: psycopg.Connection[TupleRow], *, tables: Sequence[str], first_migration: str
) -> None:
    """Raise unless each table exists and the current role neither owns it nor is its owner."""
    with conn.cursor() as cur:
        for table in tables:
            cur.execute(_SELECT_CONTROLS_TABLE, {"name": table})
            row = cur.fetchone()
            if row is None:
                raise GraphLogMigrationMissingError(first_migration, reason="table_missing")
            if row[0]:
                raise GraphLogMigrationMissingError(
                    first_migration, reason="state_role_controls_table"
                )


def _emit_verify_entry(
    *,
    outcome: str,
    extra: dict[str, object],
    emitter: LogEmitter | None,
) -> None:
    """Log one entry per verifier call: migration names and a fixed reason, never SQL."""
    with contextlib.suppress(LoggingLifecycleError):
        emit_log_entry(
            component="persistence",
            action="verify_privileged_migrations",
            outcome=outcome,
            extra=extra,
            emitter=emitter,
        )


def verify_privileged_migrations_applied(
    conn: psycopg.Connection[TupleRow],
    *,
    sources: Sequence[MigrationSource],
    required_tables: Sequence[str],
    emitter: LogEmitter | None = None,
) -> None:
    """Check, read-only and with the application role, that the privileged migrations are in place.

    Every migration file of every source must have a tracking row, every `required_tables`
    entry (schema-qualified) must exist, and the connected role must neither own a table nor be a
    member of its owner, so a deployment whose tables are owned by `ps_state` also fails closed.
    Nothing is created or changed. Emits one `persistence`/`verify_privileged_migrations` entry.

    Raises:
        GraphLogMigrationMissingError: a check failed; names the first missing migration
            (`<component>/<filename>`) and a fixed reason, never a host, SQL or driver text.
    """
    try:
        _require_migrations_recorded(conn, sources=sources)
        _require_tables_in_protected_state(
            conn, tables=required_tables, first_migration=_first_migration_name(sources)
        )
    except GraphLogMigrationMissingError as exc:
        reason: MissingMigrationReason = exc.reason
        _emit_verify_entry(
            outcome="failure",
            extra={"missing_migration": exc.missing_migration, "reason": reason},
            emitter=emitter,
        )
        raise
    _emit_verify_entry(outcome="success", extra={"tables": len(required_tables)}, emitter=emitter)
