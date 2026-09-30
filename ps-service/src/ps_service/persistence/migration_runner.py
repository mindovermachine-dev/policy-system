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
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

import psycopg

from ps_service.logging.errors import LoggingLifecycleError
from ps_service.logging.facade import emit_log_entry
from ps_service.persistence.errors import StatePostgresMigrationApplyError

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


_CREATE_TRACKING_TABLE = """
CREATE TABLE IF NOT EXISTS ps_schema_migrations (
    component text NOT NULL,
    filename text NOT NULL,
    applied_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (component, filename)
)
"""

_SELECT_ALREADY_APPLIED = (
    "SELECT 1 FROM ps_schema_migrations WHERE component = %(component)s AND filename = %(filename)s"
)
_RECORD_APPLIED = (
    "INSERT INTO ps_schema_migrations (component, filename) VALUES (%(component)s, %(filename)s)"
)


def _discover_migration_files(directory: Path) -> list[Path]:
    """Return every `*.sql` file under `directory`, sorted by filename.

    Filename order is the whole ordering contract -- `NNNN_description.sql`
    sorts correctly as plain strings as long as `NNNN` stays zero-padded and
    never exceeds four digits.
    """
    return sorted(directory.glob("*.sql"))


def _split_statements(sql: str) -> Sequence[str]:
    """Split a migration file's SQL text into individual statements.

    `psycopg` (v3) always uses PostgreSQL's extended query protocol, which
    rejects more than one command in a single `execute()` call -- unlike
    `psycopg2`'s simple-query-protocol default. Splitting on `;` is safe for
    the hand-written DDL migrations (no string literals or
    dollar-quoted bodies contain a literal `;` today); revisit this if a
    future migration ever needs one.
    """
    return [statement.strip() for statement in sql.split(";") if statement.strip()]


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


def apply_pending_migrations(
    conn: psycopg.Connection[TupleRow],
    *,
    sources: Sequence[MigrationSource],
    emitter: LogEmitter | None = None,
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

    Raises:
        StatePostgresMigrationApplyError: a migration file's SQL failed to
            apply -- names the failing component and filename; the
            underlying `psycopg.Error` is chained via `from exc`.
    """
    with conn.cursor() as cur:
        cur.execute(_CREATE_TRACKING_TABLE)
    conn.commit()

    applied: list[str] = []
    already_applied_count = 0
    for source in sources:
        for migration_file in _discover_migration_files(source.directory):
            key = {"component": source.component, "filename": migration_file.name}
            with conn.cursor() as cur:
                cur.execute(_SELECT_ALREADY_APPLIED, key)
                already_applied = cur.fetchone() is not None
            if already_applied:
                already_applied_count += 1
                continue

            sql = migration_file.read_text(encoding="utf-8")
            try:
                with conn.cursor() as cur:
                    for statement in _split_statements(sql):
                        # `statement` is dynamically split from a hand-authored, trusted
                        # migration file (never user input) -- `cast()` is unavoidable
                        # here because `psycopg`'s `execute()` typing requires
                        # `LiteralString` for its query argument, which a runtime
                        # `str.split()` result can never statically be (L2 cast() policy).
                        cur.execute(cast("LiteralString", statement))
                    cur.execute(_RECORD_APPLIED, key)
                conn.commit()
            except psycopg.Error as exc:
                conn.rollback()
                _emit_apply_entry(
                    outcome="failure",
                    applied=applied,
                    already_applied=already_applied_count,
                    emitter=emitter,
                )
                raise StatePostgresMigrationApplyError(
                    f"migration {source.component}/{migration_file.name} failed to apply: {exc}"
                ) from exc
            applied.append(f"{source.component}/{migration_file.name}")
    _emit_apply_entry(
        outcome="success", applied=applied, already_applied=already_applied_count, emitter=emitter
    )
    return [name.split("/", 1)[1] for name in applied]
