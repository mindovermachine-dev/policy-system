"""Hand-rolled, dependency-free SQL migration runner (PLAN.md §0.6, issue #131).

No ORM/Alembic (PLAN.md §0.6's own rationale: two small tables, no
foreign-key evolution complexity an ORM would meaningfully help with).
Applies every `.sql` file under `passkey_signing/migrations/` not yet
recorded in a `schema_migrations` tracking table, in filename order, each
inside its own transaction -- a second run applies nothing new (idempotent).
Wired into `create_app`'s `lifespan` startup (`ps_service.main`), gated so it
only runs when `config.passkey_signing_postgres_host` is configured.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, cast

import psycopg

from ps_service.passkey_signing.errors import MigrationApplyError

if TYPE_CHECKING:
    from collections.abc import Sequence
    from typing import LiteralString

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

    Filename order is the whole ordering contract (PLAN.md §0.6) --
    `NNNN_description.sql` sorts correctly as plain strings as long as `NNNN`
    stays zero-padded and never exceeds four digits.
    """
    return sorted(_MIGRATIONS_DIR.glob("*.sql"))


def _split_statements(sql: str) -> Sequence[str]:
    """Split a migration file's SQL text into individual statements.

    `psycopg` (v3) always uses PostgreSQL's extended query protocol, which
    rejects more than one command in a single `execute()` call ("cannot
    insert multiple commands into a prepared statement") -- unlike
    `psycopg2`'s simple-query-protocol default. Splitting on `;` is safe for
    this component's own hand-written DDL migrations (no string literals or
    dollar-quoted bodies contain a literal `;` today); revisit this if a
    future migration ever needs one.
    """
    return [statement.strip() for statement in sql.split(";") if statement.strip()]


def apply_pending_migrations(conn: psycopg.Connection[TupleRow]) -> list[str]:
    """Apply every not-yet-recorded `.sql` migration file, in filename order.

    Returns the filenames actually applied this call (empty on a repeat run
    against an already-up-to-date database -- the idempotency this function
    must guarantee). Each file's statements plus its `schema_migrations`
    bookkeeping row are committed together in one transaction, so a
    mid-file failure never leaves a migration half-applied-but-unrecorded.

    Raises:
        MigrationApplyError: a migration file's SQL failed to apply --
            names the failing filename; the underlying `psycopg.Error` is
            chained via `from exc`.
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
                    # `statement` is dynamically split from a hand-authored, trusted
                    # migration file (never user input) -- `cast()` is unavoidable
                    # here because `psycopg`'s `execute()` typing requires
                    # `LiteralString` for its query argument, which a runtime
                    # `str.split()` result can never statically be (L2 cast() policy).
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
