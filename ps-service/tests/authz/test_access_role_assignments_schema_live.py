"""Live proof of the schema the authz migrations leave behind.

Applies the shipped authz migrations to an empty schema and checks the
`access_role_assignments` table's columns, primary key and index, so an
accidental change to a migrated shape is caught.

Deselected by default; select with
`uv run pytest -m postgres_live ps-service/tests/authz/test_access_role_assignments_schema_live.py`.

Start a scratch Postgres:
`podman run -d --name <name> -p 54329:5432 -e POSTGRES_HOST_AUTH_METHOD=trust postgres:16-alpine`
and export `PS_STATE_POSTGRES_HOST`, `PS_STATE_POSTGRES_PORT`, `PS_STATE_POSTGRES_DATABASE`
and `PS_STATE_POSTGRES_USER` (e.g. `localhost`, `54329`, `postgres`, `postgres`). For the whole
`-m postgres_live` suite also export `PS_TEST_POSTGRES_SUPERUSER_DSN` (e.g.
`postgresql://postgres@localhost:54329/postgres`) with `psql` on `PATH`, because neighbouring
modules read it. `-m postgres_live` runs in a single process.

Each test works in its own uuid-named schema, dropped on teardown.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from persistence.live_schema_support import (
    ColumnShape,
    column_shapes,
    index_definitions,
    isolated_schema,
    primary_key_columns,
    require_configured_state_postgres,
)

from ps_service.authz import MIGRATIONS_DIR as AUTHZ_MIGRATIONS_DIR
from ps_service.persistence import MigrationSource, apply_pending_migrations

if TYPE_CHECKING:
    from collections.abc import Iterator

    from psycopg import Connection
    from psycopg.rows import TupleRow

pytestmark = pytest.mark.postgres_live

TABLE = "access_role_assignments"


@pytest.fixture
def migrated_conn() -> Iterator[Connection[TupleRow]]:
    require_configured_state_postgres()
    with isolated_schema() as (conn, _schema):
        apply_pending_migrations(conn, sources=[MigrationSource("authz", AUTHZ_MIGRATIONS_DIR)])
        yield conn


def test_table_has_exactly_the_expected_columns_with_types_and_nullability(
    migrated_conn: Connection[TupleRow],
) -> None:
    text_not_null = ColumnShape("text", "NO", None)
    assert column_shapes(migrated_conn, TABLE) == {
        "principal_subject": text_not_null,
        "principal_issuer": text_not_null,
        "access_role": text_not_null,
        "granted_by_subject": text_not_null,
        "granted_by_issuer": text_not_null,
        "granted_at": ColumnShape("timestamp with time zone", "NO", "now()"),
    }


def test_primary_key_is_subject_issuer_role_in_that_order(
    migrated_conn: Connection[TupleRow],
) -> None:
    assert primary_key_columns(migrated_conn, TABLE) == [
        "principal_subject",
        "principal_issuer",
        "access_role",
    ]


def test_role_index_covers_the_access_role_column(migrated_conn: Connection[TupleRow]) -> None:
    definitions = index_definitions(migrated_conn, TABLE)
    assert definitions["access_role_assignments_role_idx"].endswith("(access_role)")
