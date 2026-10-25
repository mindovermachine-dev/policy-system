"""Live proof that `live_schema_support.isolated_schema` isolates and cleans up.

Every live schema-shape and upgrade test works inside `isolated_schema`, so this
module shows the helper hands out an empty, private schema and removes it again
even when the body aborts mid-transaction.

Deselected by default; select with
`uv run pytest -m postgres_live ps-service/tests/persistence/test_live_schema_support.py`.

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

import psycopg
import pytest

from persistence.live_schema_support import (
    isolated_schema,
    require_configured_state_postgres,
    table_names,
)
from ps_service.config import load_config
from ps_service.persistence import connect_from_config

if TYPE_CHECKING:
    from psycopg import Connection
    from psycopg.rows import TupleRow

pytestmark = pytest.mark.postgres_live


@pytest.fixture(autouse=True)
def postgres_configured() -> None:
    require_configured_state_postgres()


def _schema_exists(conn: Connection[TupleRow], schema: str) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM pg_namespace WHERE nspname = %s", (schema,))
        return cur.fetchone() is not None


def _observer() -> Connection[TupleRow]:
    return connect_from_config(load_config())


def test_isolated_schema_starts_empty_and_is_dropped_on_exit() -> None:
    with isolated_schema() as (conn, schema):
        assert table_names(conn) == set()
        with conn.cursor() as cur:
            cur.execute("SELECT current_schema()")
            row = cur.fetchone()
        assert row is not None
        assert row[0] == schema
    observer = _observer()
    try:
        assert not _schema_exists(observer, schema)
    finally:
        observer.close()


def _abort_inside_a_transaction(schemas: list[str]) -> None:
    with isolated_schema() as (conn, schema):
        schemas.append(schema)
        with conn.cursor() as cur:
            cur.execute("CREATE TABLE half_done (id integer)")
            cur.execute("SELECT 1/0")


def test_schema_is_dropped_even_when_the_body_aborts_a_transaction() -> None:
    schemas: list[str] = []
    with pytest.raises(psycopg.errors.DivisionByZero):
        _abort_inside_a_transaction(schemas)
    observer = _observer()
    try:
        assert not _schema_exists(observer, schemas[0])
    finally:
        observer.close()


def test_a_following_context_sees_no_leftover_objects() -> None:
    with isolated_schema() as (first_conn, first_schema):
        with first_conn.cursor() as cur:
            cur.execute("CREATE TABLE leftover_probe (id integer)")
        first_conn.commit()
        assert table_names(first_conn) == {"leftover_probe"}
    with isolated_schema() as (second_conn, second_schema):
        assert second_schema != first_schema
        assert table_names(second_conn) == set()
