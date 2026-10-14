"""Dependency-free SQL migration runner for the passkey-signing Postgres tables.

Applies every `.sql` file under `passkey_signing/migrations/` that is not yet recorded in a
`schema_migrations` tracking table, in filename order, each inside its own transaction, so a
second run applies nothing new (idempotent). There is no ORM or migration framework because the
schema is a few small tables without evolving foreign keys. The composition root (`ps_service.main`)
runs it at application startup, only when `config.passkey_signing_postgres_host` is configured.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, cast

import psycopg

from ps_service.passkey_signing.errors import MigrationApplyError

if TYPE_CHECKING:
    from collections.abc import Sequence
    from typing import LiteralString  # noqa: V104 - named only inside a string cast

    from psycopg.rows import TupleRow

_MIGRATIONS_DIR = Path(__file__).parent / "migrations"

_CREATE_TRACKING_TABLE = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    filename text PRIMARY KEY,
    applied_at timestamptz NOT NULL DEFAULT now()
)
"""

_SELECT_ALREADY_APPLIED = "SELECT 1 FROM schema_migrations WHERE filename = %(filename)s"
_RECORD_APPLIED = "INSERT INTO schema_migrations (filename) VALUES (%(filename)s)"


def _discover_migration_files() -> list[Path]:
    """Return every `*.sql` file under `migrations/`, sorted by filename.

    Filename order is the whole ordering contract: `NNNN_description.sql` sorts correctly as
    plain strings as long as `NNNN` stays zero-padded and never exceeds four digits.
    """
    return sorted(_MIGRATIONS_DIR.glob("*.sql"))


def _split_statements(sql: str) -> Sequence[str]:
    """Split a migration file's SQL text into individual statements.

    `psycopg` (v3) uses PostgreSQL's extended query protocol, which rejects more than one command
    in a single `execute()` call. Splitting on `;` is safe for this component's hand-written DDL
    migrations only while no string literal or dollar-quoted body contains a literal `;`; a
    migration that needs one requires a smarter splitter.
    """
    return [statement.strip() for statement in sql.split(";") if statement.strip()]


def apply_pending_migrations(conn: psycopg.Connection[TupleRow]) -> list[str]:
    """Apply every not-yet-recorded `.sql` migration file, in filename order.

    Each file's statements plus its `schema_migrations` bookkeeping row are committed together in
    one transaction, so a mid-file failure never leaves a migration half-applied but unrecorded.

    Args:
        conn: An open connection to the passkey-signing Postgres database. The caller owns it
            and closes it.

    Returns:
        Filenames applied by this call, in order. Empty on a repeat run against an up-to-date
        database.

    Raises:
        MigrationApplyError: a migration file's SQL failed to apply. Names the failing filename;
            the underlying `psycopg.Error` is chained as `__cause__`. Files applied earlier in the
            call stay committed; the failing file is rolled back.
        psycopg.Error: creating the tracking table or reading it failed.

    Side Effects:
        Creates `schema_migrations` if absent, executes each pending file's DDL and inserts one
        `schema_migrations` row per applied file, committing after each; rolls back the failing
        file's transaction.
    """
    with conn.cursor() as cur:
        cur.execute(_CREATE_TRACKING_TABLE)
    conn.commit()

    applied: list[str] = []
    for migration_file in _discover_migration_files():
        with conn.cursor() as cur:
            cur.execute(_SELECT_ALREADY_APPLIED, {"filename": migration_file.name})
            already_applied = cur.fetchone() is not None
        if already_applied:
            continue

        sql = migration_file.read_text(encoding="utf-8")
        try:
            with conn.cursor() as cur:
                for statement in _split_statements(sql):
                    # `statement` comes from a hand-authored migration file, never user input;
                    # `cast()` is needed because `execute()` is typed to take a `LiteralString`,
                    # which a runtime split result cannot be statically.
                    cur.execute(cast("LiteralString", statement))
                cur.execute(_RECORD_APPLIED, {"filename": migration_file.name})
            conn.commit()
        except psycopg.Error as exc:
            conn.rollback()
            raise MigrationApplyError(
                f"migration {migration_file.name!r} failed to apply: {exc}"
            ) from exc
        applied.append(migration_file.name)
    return applied
