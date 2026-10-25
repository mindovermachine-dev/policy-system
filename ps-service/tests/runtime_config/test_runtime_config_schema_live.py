"""Live proof of the schema the runtime_config migrations leave behind.

Applies the shipped runtime_config migrations to an empty schema and checks the
`runtime_config` table's columns and primary key, so an accidental change to a
migrated shape is caught.

Deselected by default; select with
`uv run pytest -m postgres_live ps-service/tests/runtime_config/test_runtime_config_schema_live.py`.

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
    isolated_schema,
    primary_key_columns,
    require_configured_state_postgres,
)

from ps_service.persistence import MigrationSource, apply_pending_migrations
from ps_service.runtime_config import MIGRATIONS_DIR as RUNTIME_CONFIG_MIGRATIONS_DIR

if TYPE_CHECKING:
    from collections.abc import Iterator

    from psycopg import Connection
    from psycopg.rows import TupleRow

pytestmark = pytest.mark.postgres_live

TABLE = "runtime_config"


@pytest.fixture
def migrated_conn() -> Iterator[Connection[TupleRow]]:
    require_configured_state_postgres()
    with isolated_schema() as (conn, _schema):
        apply_pending_migrations(
            conn, sources=[MigrationSource("runtime_config", RUNTIME_CONFIG_MIGRATIONS_DIR)]
        )
        yield conn


def test_primary_key_is_the_key_column(migrated_conn: Connection[TupleRow]) -> None:
    assert primary_key_columns(migrated_conn, TABLE) == ["key"]


def test_value_is_a_required_jsonb_column(migrated_conn: Connection[TupleRow]) -> None:
    shape = column_shapes(migrated_conn, TABLE)["value"]
    assert (shape.data_type, shape.is_nullable) == ("jsonb", "NO")


def test_updated_at_is_a_required_timestamp_defaulting_to_now(
    migrated_conn: Connection[TupleRow],
) -> None:
    assert column_shapes(migrated_conn, TABLE)["updated_at"] == ColumnShape(
        "timestamp with time zone", "NO", "now()"
    )


def test_table_has_exactly_the_key_value_and_updated_at_columns(
    migrated_conn: Connection[TupleRow],
) -> None:
    assert set(column_shapes(migrated_conn, TABLE)) == {"key", "value", "updated_at"}
