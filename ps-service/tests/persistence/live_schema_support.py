"""Shared support for live tests that inspect a state-database schema.

`isolated_schema` hands out a connection pinned to a brand-new, uniquely-named
Postgres schema and always drops it afterwards. The inspection helpers read the
schema the connection is pinned to (`current_schema()`), so what they report is
exactly what the migrations under test created.

Requires `PS_STATE_POSTGRES_*` to point at a reachable Postgres.
"""

from __future__ import annotations

import re
import shutil
import uuid
from contextlib import contextmanager
from typing import TYPE_CHECKING, NamedTuple, cast

from ps_service.config import load_config
from ps_service.passkey_signing import migration_runner as passkey_runner_module
from ps_service.persistence import connect_from_config

if TYPE_CHECKING:
    from collections.abc import Generator
    from pathlib import Path
    from typing import LiteralString

    import pytest
    from psycopg import Connection
    from psycopg.rows import TupleRow


class ColumnShape(NamedTuple):
    """The comparable shape of one column."""

    data_type: str
    is_nullable: str
    column_default: str | None


class SchemaSnapshot(NamedTuple):
    """Everything about a schema's structure that two equal schemas must share.

    Ordinal positions, row data and `applied_at` values are deliberately left out.
    """

    columns: tuple[tuple[str, str, str, str, str | None], ...]
    constraints: tuple[tuple[str, str, str], ...]
    indexes: tuple[tuple[str, str, str], ...]


def require_configured_state_postgres() -> None:
    """Fail loudly when no state Postgres is configured for a live test."""
    config = load_config()
    assert config.state_postgres_host is not None, (
        "postgres_live requires PS_STATE_POSTGRES_HOST to be set"
    )


@contextmanager
def isolated_schema() -> Generator[tuple[Connection[TupleRow], str]]:
    """Yield `(connection, schema_name)` pinned to a fresh, uniquely-named schema.

    The schema name is `uuid4`-generated, never external input, so it is safe to
    interpolate. On exit any open transaction is rolled back before the schema is
    dropped, so a body that aborted mid-transaction still leaves nothing behind.
    """
    conn = connect_from_config(load_config())
    schema = f"migration_test_{uuid.uuid4().hex}"
    try:
        with conn.cursor() as cur:
            cur.execute(cast("LiteralString", f'CREATE SCHEMA "{schema}"'))
            cur.execute(cast("LiteralString", f'SET search_path TO "{schema}"'))
        conn.commit()
        yield conn, schema
    finally:
        conn.rollback()
        with conn.cursor() as cur:
            cur.execute(cast("LiteralString", f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        conn.commit()
        conn.close()


def table_names(conn: Connection[TupleRow]) -> set[str]:
    """Names of the tables in the connection's current schema."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = current_schema() ORDER BY table_name"
        )
        return {row[0] for row in cur.fetchall()}


def column_shapes(conn: Connection[TupleRow], table: str) -> dict[str, ColumnShape]:
    """Column name to type, nullability and default for `table` in the current schema."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT column_name, data_type, is_nullable, column_default "
            "FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = %s "
            "ORDER BY column_name",
            (table,),
        )
        return {row[0]: ColumnShape(row[1], row[2], row[3]) for row in cur.fetchall()}


def primary_key_columns(conn: Connection[TupleRow], table: str) -> list[str]:
    """Primary-key column names of `table`, in key order."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT kcu.column_name "
            "FROM information_schema.table_constraints tc "
            "JOIN information_schema.key_column_usage kcu "
            "  ON kcu.constraint_name = tc.constraint_name "
            " AND kcu.constraint_schema = tc.constraint_schema "
            " AND kcu.table_name = tc.table_name "
            "WHERE tc.constraint_type = 'PRIMARY KEY' "
            "  AND tc.table_schema = current_schema() AND tc.table_name = %s "
            "ORDER BY kcu.ordinal_position",
            (table,),
        )
        return [row[0] for row in cur.fetchall()]


def strip_schema_qualifier(definition: str, schema: str) -> str:
    """Remove the generated schema qualifier so definitions compare across schemas."""
    return re.sub(rf'"?{re.escape(schema)}"?\.', "", definition)


def index_definitions(conn: Connection[TupleRow], table: str) -> dict[str, str]:
    """Index name to definition for `table`, with the schema qualifier removed."""
    with conn.cursor() as cur:
        cur.execute("SELECT current_schema()")
        schema_row = cur.fetchone()
        assert schema_row is not None
        schema: str = schema_row[0]
        cur.execute(
            "SELECT indexname, indexdef FROM pg_indexes "
            "WHERE schemaname = current_schema() AND tablename = %s "
            "ORDER BY indexname",
            (table,),
        )
        return {row[0]: strip_schema_qualifier(row[1], schema) for row in cur.fetchall()}


def schema_snapshot(conn: Connection[TupleRow], schema: str) -> SchemaSnapshot:
    """Capture the structure of `schema` with its own name stripped from every definition.

    `schema` must be the schema the connection is pinned to. Every list is sorted, so two
    snapshots compare equal exactly when the structures match.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT table_name, column_name, data_type, is_nullable, column_default "
            "FROM information_schema.columns WHERE table_schema = current_schema() "
            "ORDER BY table_name, column_name"
        )
        columns = tuple(
            (row[0], row[1], row[2], row[3], _strip_optional(row[4], schema))
            for row in cur.fetchall()
        )
        cur.execute(
            "SELECT conrelid::regclass::text AS tbl, conname, pg_get_constraintdef(oid) "
            "FROM pg_constraint WHERE connamespace = current_schema()::regnamespace "
            "ORDER BY 1, 2"
        )
        constraints = tuple(
            (
                strip_schema_qualifier(row[0], schema),
                row[1],
                strip_schema_qualifier(row[2], schema),
            )
            for row in cur.fetchall()
        )
        cur.execute(
            "SELECT tablename, indexname, indexdef FROM pg_indexes "
            "WHERE schemaname = current_schema() ORDER BY tablename, indexname"
        )
        indexes = tuple(
            (row[0], row[1], strip_schema_qualifier(row[2], schema)) for row in cur.fetchall()
        )
    return SchemaSnapshot(columns, constraints, indexes)


def _strip_optional(definition: str | None, schema: str) -> str | None:
    return None if definition is None else strip_schema_qualifier(definition, schema)


def copy_migration_prefix(source_dir: Path, count: int, destination: Path) -> Path:
    """Copy the first `count` migration files of `source_dir`, in filename order.

    Returns `destination`, created if absent, ready to be used as a migration directory.
    """
    destination.mkdir(parents=True, exist_ok=True)
    for migration_file in sorted(source_dir.glob("*.sql"))[:count]:
        shutil.copy(migration_file, destination / migration_file.name)
    return destination


def passkey_migrations_dir() -> Path:
    """The directory the passkey signing runner applies by default."""
    return passkey_runner_module._MIGRATIONS_DIR  # pyright: ignore[reportPrivateUsage]


def apply_passkey_directory(
    conn: Connection[TupleRow], directory: Path, monkeypatch: pytest.MonkeyPatch
) -> list[str]:
    """Apply the passkey signing migrations found in `directory`; return the applied filenames.

    The directory stays in effect until `monkeypatch` undoes it at test teardown.
    """
    # detroit-exception: the passkey runner takes its migration directory only from a module
    # constant, with no parameter, so repointing it is the only way to apply a file prefix
    monkeypatch.setattr(passkey_runner_module, "_MIGRATIONS_DIR", directory)
    return passkey_runner_module.apply_pending_migrations(conn)
