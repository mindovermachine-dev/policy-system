"""Tests for the `ingestion_runs` migration (issue #194)."""

from __future__ import annotations

import re
import uuid
from typing import TYPE_CHECKING, cast

import psycopg
import pytest

from ps_service.audit import MIGRATIONS_DIR as AUDIT_MIGRATIONS_DIR
from ps_service.authz import MIGRATIONS_DIR as AUTHZ_MIGRATIONS_DIR
from ps_service.config import load_config
from ps_service.ingestion_runs import MIGRATIONS_DIR as INGESTION_RUNS_MIGRATIONS_DIR
from ps_service.persistence import MigrationSource, apply_pending_migrations, connect_from_config
from ps_service.persistence.migration_runner import (
    _split_statements,  # pyright: ignore[reportPrivateUsage]  -- test-only reuse so the semicolon-in-comment guard checks the runner's own statement-splitting logic
)
from ps_service.runtime_config import MIGRATIONS_DIR as RUNTIME_CONFIG_MIGRATIONS_DIR

if TYPE_CHECKING:
    from typing import LiteralString

_CREATE_TABLE = re.compile(r"^\s*CREATE\s+TABLE\s+(\w+)", re.IGNORECASE | re.MULTILINE)
_SOURCES = [
    MigrationSource("audit", AUDIT_MIGRATIONS_DIR),
    MigrationSource("authz", AUTHZ_MIGRATIONS_DIR),
    MigrationSource("runtime_config", RUNTIME_CONFIG_MIGRATIONS_DIR),
    MigrationSource("ingestion_runs", INGESTION_RUNS_MIGRATIONS_DIR),
]
_FILE = INGESTION_RUNS_MIGRATIONS_DIR / "0001_ingestion_runs.sql"


def test_the_migration_file_creates_exactly_the_ingestion_runs_table() -> None:
    sql = _FILE.read_text(encoding="utf-8")

    assert set(_CREATE_TABLE.findall(sql)) == {"ingestion_runs"}
    assert [path.name for path in INGESTION_RUNS_MIGRATIONS_DIR.glob("*.sql")] == [_FILE.name]


def test_the_migration_file_carries_the_append_only_rule() -> None:
    sql = _FILE.read_text(encoding="utf-8")

    assert "append-only from the first deployment onward" in sql


def test_the_migration_file_has_no_semicolon_inside_a_comment() -> None:
    sql = _FILE.read_text(encoding="utf-8")
    code_only = re.sub(r"--[^\n]*", "", sql)

    assert len(_split_statements(sql)) == code_only.count(";")


def _isolated() -> tuple[psycopg.Connection[tuple[object, ...]], str]:
    config = load_config()
    assert config.state_postgres_host is not None, (
        "postgres_live requires PS_STATE_POSTGRES_HOST to be set"
    )
    conn = connect_from_config(config)
    schema = f"migration_test_{uuid.uuid4().hex}"
    conn.execute(cast("LiteralString", f'CREATE SCHEMA "{schema}"'))
    conn.execute(cast("LiteralString", f'SET search_path TO "{schema}"'))
    conn.commit()
    return cast("psycopg.Connection[tuple[object, ...]]", conn), schema


def _drop(conn: psycopg.Connection[tuple[object, ...]], schema: str) -> None:
    conn.rollback()
    conn.execute(cast("LiteralString", f'DROP SCHEMA "{schema}" CASCADE'))
    conn.commit()
    conn.close()


@pytest.mark.postgres_live
def test_migration_applies_once_and_a_second_run_applies_nothing() -> None:
    conn, schema = _isolated()
    try:
        first = apply_pending_migrations(conn, sources=_SOURCES)
        second = apply_pending_migrations(conn, sources=_SOURCES)
    finally:
        _drop(conn, schema)

    assert "0001_ingestion_runs.sql" in first
    assert second == []


@pytest.mark.postgres_live
def test_check_constraints_reject_an_unknown_status_and_a_succeeded_row_without_result() -> None:
    conn, schema = _isolated()
    try:
        apply_pending_migrations(conn, sources=_SOURCES)
        insert = (
            "INSERT INTO ingestion_runs (run_id, celex, short_name, actor_subject, actor_issuer,"
            " status, finished_at) VALUES (%(id)s, 'c', 's', 'a', 'i', %(status)s, now())"
        )
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(insert, {"id": str(uuid.uuid4()), "status": "bogus"})
        conn.rollback()
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(insert, {"id": str(uuid.uuid4()), "status": "succeeded"})
    finally:
        _drop(conn, schema)
