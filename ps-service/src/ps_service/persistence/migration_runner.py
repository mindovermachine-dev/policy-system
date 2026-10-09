"""Hand-rolled, dependency-free SQL migration runner for the PS state Postgres (issue #130, #133).

A structural copy of `ps_service.passkey_signing.migration_runner`'s pattern
-- own `ps_schema_migrations` tracking table, keyed `(component, filename)`
(deliberately not the bare `schema_migrations` name passkey_signing's own
runner creates, so the two runners can never silently collide even if an
operator ever points both at the same physical Postgres instance/database).

The runner is component-agnostic: the composition root (`ps_service.main`)
passes explicit `MigrationSource(component, directory)` entries, so this
package never imports a component package. For each source, in list order,
it applies every `.sql` file under `directory` not yet recorded for that
component, in filename order, each inside its own transaction -- a second
run applies nothing new (idempotent). Wired into `create_app`'s `lifespan`
startup, gated so it only runs when `config.state_postgres_host` is
configured.

Migrations are append-only from the first deployment onward.

Never edit, renumber or delete an applied migration file: add a new,
higher-numbered file instead. The runner records each applied file by `(component, filename)`
and never re-runs or re-checks it, so an edited applied file silently
diverges from every database that already ran it. Before the first
deployment (issue #130) the history was collapsed to one baseline file per
component; after it, that freedom is gone.
"""

from __future__ import annotations

import contextlib
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

import psycopg
from psycopg import sql

from ps_service.logging.errors import LoggingLifecycleError
from ps_service.logging.facade import emit_log_entry
from ps_service.persistence.errors import (
    StatePostgresMigrationApplyError,
    StatePostgresMigrationLockError,
    StatePostgresProvisioningError,
)

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path
    from typing import LiteralString

    from psycopg.rows import TupleRow

    from ps_service.logging.emitter import LogEmitter


@dataclass(frozen=True)
class MigrationSource:
    """One component's migration directory, as named by the composition root."""

    component: str
    directory: Path


CREATE_TRACKING_TABLE = """
CREATE TABLE IF NOT EXISTS ps_schema_migrations (
    component text NOT NULL,
    filename text NOT NULL,
    applied_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (component, filename)
)
"""

MIGRATION_LOCK_KEY = 0x5053_4D49_4752_4154
"""Advisory-lock key serializing migration runners (ASCII "PSMIGRAT").

Deliberately outside the int32 range: the other advisory-lock users in this service key on
`hashtext(...)`, an int4 sign-extended into the same bigint key space, so a constant that fits in
int32 could collide with one of them.
"""

DEFAULT_LOCK_TIMEOUT_SECONDS = 60.0
"""How long a runner waits for `MIGRATION_LOCK_KEY` before failing with a lock error."""

_SELECT_TRACKING_TABLE_EXISTS = "SELECT to_regclass('ps_schema_migrations') IS NOT NULL"

SELECT_ALREADY_APPLIED = (
    "SELECT 1 FROM ps_schema_migrations WHERE component = %(component)s AND filename = %(filename)s"
)
RECORD_APPLIED = (
    "INSERT INTO ps_schema_migrations (component, filename) VALUES (%(component)s, %(filename)s)"
)


_SPLIT_TOKEN = re.compile(r";|\$[A-Za-z_]?[A-Za-z0-9_]*\$")


def discover_migration_files(directory: Path) -> list[Path]:
    """Return every `*.sql` file under `directory`, sorted by filename.

    Filename order is the whole ordering contract -- `NNNN_description.sql`
    sorts correctly as plain strings as long as `NNNN` stays zero-padded and
    never exceeds four digits.
    """
    return sorted(directory.glob("*.sql"))


def split_statements(sql: str) -> Sequence[str]:
    """Split a migration file's SQL text into individual statements.

    `psycopg` (v3) always uses PostgreSQL's extended query protocol, which
    rejects more than one command in a single `execute()` call -- unlike
    `psycopg2`'s simple-query-protocol default. Splitting on `;` is safe for
    the hand-written DDL migrations as long as no string literal or comment
    contains a literal `;`. A dollar-quoted body (`$$ ... $$` or `$tag$ ... $tag$`,
    as used by a PL/pgSQL trigger function) is kept whole, so its inner `;`
    never splits the statement.
    """
    statements: list[str] = []
    statement_start = 0
    open_quote: str | None = None
    for match in _SPLIT_TOKEN.finditer(sql):
        matched = match.group()
        if open_quote is not None:
            open_quote = None if matched == open_quote else open_quote
        elif matched == ";":
            statements.append(sql[statement_start : match.start()])
            statement_start = match.end()
        else:
            open_quote = matched
    statements.append(sql[statement_start:])
    return [statement.strip() for statement in statements if statement.strip()]


def _emit_apply_entry(
    *,
    outcome: str,
    applied: Sequence[str],
    already_applied: int,
    emitter: LogEmitter | None,
) -> None:
    """Log one entry per runner call: filenames only, never SQL or connection details."""
    # A process without a configured default emitter (e.g. a bare script) must
    # still be able to migrate: the entry is diagnostics, not a precondition.
    with contextlib.suppress(LoggingLifecycleError):
        emit_log_entry(
            component="persistence",
            action="apply_migrations",
            outcome=outcome,
            extra={"applied": list(applied), "already_applied": already_applied},
            emitter=emitter,
        )


def acquire_migration_lock(
    conn: psycopg.Connection[TupleRow], *, timeout_seconds: float, subject: str
) -> None:
    """Take `MIGRATION_LOCK_KEY` for the connection's current transaction, waiting a bounded time.

    The lock is transaction-scoped: it is released by the commit or rollback that ends the
    transaction. The wait is bounded by `lock_timeout`, which is reset to the session default as
    soon as the lock is held so later DDL lock waits behave as before.

    Raises:
        StatePostgresMigrationLockError: the lock was not granted within `timeout_seconds`
            (`subject` names what was being done; the transaction is rolled back).
    """
    milliseconds = max(1, round(timeout_seconds * 1000))
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT set_config('lock_timeout', %s, true)", (f"{milliseconds}ms",))
            cur.execute("SELECT pg_advisory_xact_lock(%s)", (MIGRATION_LOCK_KEY,))
            cur.execute("SET LOCAL lock_timeout = DEFAULT")
    except psycopg.errors.LockNotAvailable as exc:
        conn.rollback()
        message = f"{subject} could not acquire the migration lock within {timeout_seconds:g}s"
        raise StatePostgresMigrationLockError(message) from exc


def set_local_role(conn: psycopg.Connection[TupleRow], role: str) -> None:
    """Switch the current transaction to `role` (`SET LOCAL ROLE`), classifying a refusal.

    Must be the first statement of the transaction it applies to. The switch ends with the
    transaction, so it never leaks to later work on the same connection.

    Raises:
        StatePostgresProvisioningError: the connecting role may not switch to `role` (names the
            role only, never driver text); the aborted transaction is rolled back first.
    """
    try:
        with conn.cursor() as cur:
            cur.execute(sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(role)))
    except psycopg.Error as exc:
        conn.rollback()
        message = f"admin connection cannot SET ROLE {role}"
        raise StatePostgresProvisioningError(message) from exc


def ensure_tracking_table(
    conn: psycopg.Connection[TupleRow],
    *,
    lock_timeout_seconds: float,
    run_as_role: str | None = None,
) -> None:
    """Create `ps_schema_migrations` if absent; lock-free when it already exists.

    Concurrent `CREATE TABLE IF NOT EXISTS` can itself fail, so creation takes the migration
    lock; the common case (the table exists) never waits for it. With `run_as_role` the table is
    created under that role (so it owns the table), the switch preceding the lock.
    """
    with conn.cursor() as cur:
        cur.execute(_SELECT_TRACKING_TABLE_EXISTS)
        row = cur.fetchone()
    conn.rollback()
    if row is not None and row[0]:
        return
    if run_as_role is not None:
        set_local_role(conn, run_as_role)
    acquire_migration_lock(
        conn, timeout_seconds=lock_timeout_seconds, subject="migration tracking table setup"
    )
    with conn.cursor() as cur:
        cur.execute(CREATE_TRACKING_TABLE)
    conn.commit()


def is_migration_applied(conn: psycopg.Connection[TupleRow], key: dict[str, str]) -> bool:
    """Whether the tracking table has a row for `key` (rows are never un-applied)."""
    with conn.cursor() as cur:
        cur.execute(SELECT_ALREADY_APPLIED, key)
        return cur.fetchone() is not None


def apply_migration_file(
    conn: psycopg.Connection[TupleRow],
    *,
    key: dict[str, str],
    statements: Sequence[str],
    lock_timeout_seconds: float,
    run_as_role: str | None = None,
) -> bool:
    """Apply one pending migration file under the migration lock; return whether it was applied.

    In one transaction: take the lock, re-check the tracking table (another runner may have
    applied the file while this one waited), run `statements`, record the file, commit. Returns
    `False` (nothing changed) when the re-check finds the file already recorded. With
    `run_as_role` the transaction first switches to that role (before the lock), so the objects
    the statements create are owned by it.

    Raises:
        StatePostgresProvisioningError: `run_as_role` is set and the connection may not switch
            to it.
        StatePostgresMigrationLockError: the lock was not granted in time.
        StatePostgresMigrationApplyError: a statement failed; the underlying `psycopg.Error` is
            chained via `from exc` and the transaction is rolled back.
    """
    name = f"migration {key['component']}/{key['filename']}"
    if run_as_role is not None:
        set_local_role(conn, run_as_role)
    acquire_migration_lock(conn, timeout_seconds=lock_timeout_seconds, subject=name)
    try:
        if is_migration_applied(conn, key):
            conn.rollback()
            return False
        with conn.cursor() as cur:
            for statement in statements:
                # `statement` is dynamically split from a hand-authored, trusted
                # migration file (never user input) -- `cast()` is unavoidable
                # here because `psycopg`'s `execute()` typing requires
                # `LiteralString` for its query argument, which a runtime
                # `str.split()` result can never statically be (L2 cast() policy).
                cur.execute(cast("LiteralString", statement))
            cur.execute(RECORD_APPLIED, key)
        conn.commit()
    except psycopg.Error as exc:
        conn.rollback()
        raise StatePostgresMigrationApplyError(f"{name} failed to apply: {exc}") from exc
    return True


def apply_pending_migrations(
    conn: psycopg.Connection[TupleRow],
    *,
    sources: Sequence[MigrationSource],
    emitter: LogEmitter | None = None,
    lock_timeout_seconds: float = DEFAULT_LOCK_TIMEOUT_SECONDS,
    run_as_role: str | None = None,
) -> list[str]:
    """Apply every not-yet-recorded `.sql` migration file of every source, in list order.

    Returns the filenames actually applied this call (empty on a repeat run
    against an already-up-to-date database -- the idempotency this function
    must guarantee). Emits one `persistence`/`apply_migrations` log entry per
    call (`success` or `failure`) naming the `<component>/<filename>` files
    applied and the count already applied; `emitter` defaults to the process
    emitter. Each file's statements plus its `ps_schema_migrations`
    bookkeeping row are committed together in one transaction, so a
    mid-file failure never leaves a migration half-applied-but-unrecorded.

    Concurrent runners (the service and the provisioning command) are serialized per pending
    file by `MIGRATION_LOCK_KEY` and re-check the tracking table inside the lock; a runner with
    nothing pending never touches the lock. The wait for the lock is bounded by
    `lock_timeout_seconds`.

    `run_as_role` (default `None`: the service path, byte-for-byte unchanged) makes an
    ADMIN connection create everything as that role -- each transaction starts with
    `SET LOCAL ROLE <role>` -- so the application role owns every table, exactly as when it
    applied the migrations itself. The provisioning command uses it.

    Raises:
        StatePostgresProvisioningError: `run_as_role` is set and the connection may not switch
            to it.
        StatePostgresMigrationLockError: the migration lock was not granted within
            `lock_timeout_seconds`.
        StatePostgresMigrationApplyError: a migration file's SQL failed to
            apply -- names the failing component and filename; the
            underlying `psycopg.Error` is chained via `from exc`.
    """
    applied: list[str] = []
    already_applied_count = 0
    try:
        ensure_tracking_table(
            conn, lock_timeout_seconds=lock_timeout_seconds, run_as_role=run_as_role
        )
        for source in sources:
            for migration_file in discover_migration_files(source.directory):
                key = {"component": source.component, "filename": migration_file.name}
                if not is_migration_applied(conn, key) and apply_migration_file(
                    conn,
                    key=key,
                    statements=split_statements(migration_file.read_text(encoding="utf-8")),
                    lock_timeout_seconds=lock_timeout_seconds,
                    run_as_role=run_as_role,
                ):
                    applied.append(f"{source.component}/{migration_file.name}")
                else:
                    already_applied_count += 1
    except StatePostgresMigrationApplyError, StatePostgresProvisioningError:
        _emit_apply_entry(
            outcome="failure",
            applied=applied,
            already_applied=already_applied_count,
            emitter=emitter,
        )
        raise
    _emit_apply_entry(
        outcome="success", applied=applied, already_applied=already_applied_count, emitter=emitter
    )
    return [name.split("/", 1)[1] for name in applied]
