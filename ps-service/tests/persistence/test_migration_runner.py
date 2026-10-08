"""Tests for `ps_service.persistence.migration_runner` (issue #130 Slice 2, #144).

Fast tests inspect the shipped migration files and the runner's module
documentation. `postgres_live`-marked tests prove what only a real Postgres
can: that the baseline files apply, are idempotent, are applied per file
transactionally, and leave exactly the expected schema. Deselected by default
(BASELINE.md's tier gating) -- run explicitly with
`uv run pytest -m postgres_live` against a reachable `PS_STATE_POSTGRES_*`
instance (e.g. `postgres:16-alpine`, matching
`psServiceSigning.postgres.image` in `charts/policy-system/values.yaml`).

Every live test runs against its own throwaway Postgres schema
(`_isolated_connection`) so "fresh database" is observable no matter what
other live tests already applied to the shared `public` schema.
"""

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
from ps_service.persistence import (
    MigrationSource,
    StatePostgresMigrationApplyError,
    apply_pending_migrations,
    connect_from_config,
    migration_runner,
)
from ps_service.persistence.migration_runner import (
    _split_statements,  # pyright: ignore[reportPrivateUsage]  -- test-only reuse so the semicolon-in-comment guard checks the runner's own statement-splitting logic
)
from ps_service.runtime_config import MIGRATIONS_DIR as RUNTIME_CONFIG_MIGRATIONS_DIR

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path
    from typing import LiteralString

    from psycopg.rows import TupleRow

    from ps_service.config import ServiceConfig
    from ps_service.logging.emitter import LogEmitter

# Mirrors the source list `ps_service.main` passes to the runner at startup.
STATE_MIGRATION_SOURCES = [
    MigrationSource("audit", AUDIT_MIGRATIONS_DIR),
    MigrationSource("authz", AUTHZ_MIGRATIONS_DIR),
    MigrationSource("runtime_config", RUNTIME_CONFIG_MIGRATIONS_DIR),
    MigrationSource("ingestion_runs", INGESTION_RUNS_MIGRATIONS_DIR),
]

_CREATE_TABLE = re.compile(r"^\s*CREATE\s+TABLE\s+(\w+)", re.IGNORECASE | re.MULTILINE)


def _require_configured_postgres() -> None:
    config = load_config()
    assert config.state_postgres_host is not None, (
        "postgres_live requires PS_STATE_POSTGRES_HOST to be set"
    )


def _isolated_connection(config: ServiceConfig) -> tuple[psycopg.Connection[TupleRow], str]:
    """Open a connection pinned (via `search_path`) to a brand-new, uniquely-named schema.

    Returns `(conn, schema_name)`; pass both to `_cleanup_isolated_schema`.
    The schema name is `uuid4`-generated, never external input -- safe to
    interpolate directly.
    """
    conn = connect_from_config(config)
    schema = f"migration_test_{uuid.uuid4().hex}"
    with conn.cursor() as cur:
        cur.execute(cast("LiteralString", f'CREATE SCHEMA "{schema}"'))
        cur.execute(cast("LiteralString", f'SET search_path TO "{schema}"'))
    conn.commit()
    return conn, schema


def _cleanup_isolated_schema(conn: psycopg.Connection[TupleRow], schema: str) -> None:
    with conn.cursor() as cur:
        cur.execute(cast("LiteralString", f'DROP SCHEMA "{schema}" CASCADE'))
    conn.commit()
    conn.close()


def _table_names(conn: psycopg.Connection[TupleRow]) -> set[str]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = current_schema()"
        )
        return {row[0] for row in cur.fetchall()}


def _tracking_rows(conn: psycopg.Connection[TupleRow]) -> set[tuple[str, str]]:
    with conn.cursor() as cur:
        cur.execute("SELECT component, filename FROM ps_schema_migrations")
        return {(row[0], row[1]) for row in cur.fetchall()}


def _tables_created_by(directory: Path) -> dict[str, set[str]]:
    return {
        sql_file.name: set(_CREATE_TABLE.findall(sql_file.read_text(encoding="utf-8")))
        for sql_file in sorted(directory.glob("*.sql"))
    }


def _write_migration(directory: Path, filename: str, sql: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / filename).write_text(sql, encoding="utf-8")


# ---------------------------------------------------------------------------
# Fast tests: the shipped files and the documentation deliverable
# ---------------------------------------------------------------------------


def test_each_component_migration_creates_only_its_own_tables() -> None:
    assert _tables_created_by(AUDIT_MIGRATIONS_DIR) == {"0001_audit_events.sql": {"audit_events"}}
    assert _tables_created_by(AUTHZ_MIGRATIONS_DIR) == {
        "0001_access_role_assignments.sql": {"access_role_assignments"}
    }
    assert _tables_created_by(RUNTIME_CONFIG_MIGRATIONS_DIR) == {
        "0001_runtime_config.sql": {"runtime_config"}
    }
    assert _tables_created_by(INGESTION_RUNS_MIGRATIONS_DIR) == {
        "0001_ingestion_runs.sql": {"ingestion_runs"}
    }


def test_migration_sql_files_contain_no_semicolon_inside_comments() -> None:
    """The runner splits on `;`, so a `;` in a comment would run a bogus statement."""
    for source in STATE_MIGRATION_SOURCES:
        for sql_file in sorted(source.directory.glob("*.sql")):
            sql = sql_file.read_text(encoding="utf-8")
            code_only = re.sub(r"--[^\n]*", "", sql)
            assert len(_split_statements(sql)) == code_only.count(";"), sql_file


def test_migration_runner_module_documents_append_only_rule() -> None:
    doc = migration_runner.__doc__
    assert doc is not None
    assert "append-only from the first deployment onward" in doc
    assert "Never edit, renumber or delete" in doc


def test_first_migration_file_of_each_component_carries_the_append_only_rule() -> None:
    for source in STATE_MIGRATION_SOURCES:
        first = min(source.directory.glob("*.sql"))
        assert "append-only from the first deployment onward" in first.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Live tests
# ---------------------------------------------------------------------------


@pytest.mark.postgres_live
def test_empty_database_gets_one_migration_per_component_and_no_legacy_objects() -> None:
    _require_configured_postgres()
    conn, schema = _isolated_connection(load_config())
    try:
        applied = apply_pending_migrations(conn, sources=STATE_MIGRATION_SOURCES)
        tables = _table_names(conn)
        tracked = _tracking_rows(conn)
    finally:
        _cleanup_isolated_schema(conn, schema)

    assert applied == [
        "0001_audit_events.sql",
        "0001_access_role_assignments.sql",
        "0001_runtime_config.sql",
        "0001_ingestion_runs.sql",
    ]
    assert tables == {
        "audit_events",
        "access_role_assignments",
        "runtime_config",
        "ingestion_runs",
        "ps_schema_migrations",
    }
    assert tracked == {
        ("audit", "0001_audit_events.sql"),
        ("authz", "0001_access_role_assignments.sql"),
        ("runtime_config", "0001_runtime_config.sql"),
        ("ingestion_runs", "0001_ingestion_runs.sql"),
    }


@pytest.mark.postgres_live
def test_second_run_applies_nothing() -> None:
    _require_configured_postgres()
    conn, schema = _isolated_connection(load_config())
    try:
        apply_pending_migrations(conn, sources=STATE_MIGRATION_SOURCES)
        second = apply_pending_migrations(conn, sources=STATE_MIGRATION_SOURCES)
        tracked = _tracking_rows(conn)
    finally:
        _cleanup_isolated_schema(conn, schema)

    assert second == []
    assert len(tracked) == 4


@pytest.mark.postgres_live
def test_sources_apply_in_list_order(tmp_path: Path) -> None:
    _write_migration(tmp_path / "a", "0001_parent.sql", "CREATE TABLE parent (id int PRIMARY KEY)")
    _write_migration(
        tmp_path / "b", "0001_child.sql", "CREATE TABLE child (pid int REFERENCES parent (id))"
    )
    _require_configured_postgres()
    conn, schema = _isolated_connection(load_config())
    try:
        applied = apply_pending_migrations(
            conn,
            sources=[
                MigrationSource("a", tmp_path / "a"),
                MigrationSource("b", tmp_path / "b"),
            ],
        )
    finally:
        _cleanup_isolated_schema(conn, schema)

    assert applied == ["0001_parent.sql", "0001_child.sql"]


@pytest.mark.postgres_live
def test_failing_migration_file_leaves_no_partial_schema_and_no_tracking_row(
    tmp_path: Path,
) -> None:
    _write_migration(tmp_path, "0001_poisoned.sql", "CREATE TABLE ok (id int); SELECT 1/0")
    _require_configured_postgres()
    conn, schema = _isolated_connection(load_config())
    try:
        with pytest.raises(StatePostgresMigrationApplyError, match="poisoned"):
            apply_pending_migrations(conn, sources=[MigrationSource("poison", tmp_path)])
        tables = _table_names(conn)
        tracked = _tracking_rows(conn)
    finally:
        _cleanup_isolated_schema(conn, schema)

    assert "ok" not in tables
    assert tracked == set()


@pytest.mark.postgres_live
def test_apply_emits_success_entry_naming_applied_files(
    make_emitter: Callable[[], tuple[LogEmitter, Path]],
    read_lines: Callable[[Path], list[dict[str, object]]],
) -> None:
    emitter, log_path = make_emitter()
    _require_configured_postgres()
    conn, schema = _isolated_connection(load_config())
    try:
        apply_pending_migrations(conn, sources=STATE_MIGRATION_SOURCES, emitter=emitter)
        apply_pending_migrations(conn, sources=STATE_MIGRATION_SOURCES, emitter=emitter)
    finally:
        _cleanup_isolated_schema(conn, schema)
    emitter.flush()

    first, second = read_lines(log_path)
    assert first["component"] == "persistence"
    assert first["action"] == "apply_migrations"
    assert first["outcome"] == "success"
    assert first["applied"] == [
        "audit/0001_audit_events.sql",
        "authz/0001_access_role_assignments.sql",
        "runtime_config/0001_runtime_config.sql",
        "ingestion_runs/0001_ingestion_runs.sql",
    ]
    assert first["already_applied"] == 0
    assert second["applied"] == []
    assert second["already_applied"] == 4


@pytest.mark.postgres_live
def test_apply_emits_failure_entry_without_sql_before_raising(
    tmp_path: Path,
    make_emitter: Callable[[], tuple[LogEmitter, Path]],
    read_lines: Callable[[Path], list[dict[str, object]]],
) -> None:
    _write_migration(tmp_path, "0001_poisoned.sql", "SELECT 1/0")
    emitter, log_path = make_emitter()
    _require_configured_postgres()
    conn, schema = _isolated_connection(load_config())
    try:
        with pytest.raises(StatePostgresMigrationApplyError):
            apply_pending_migrations(
                conn, sources=[MigrationSource("poison", tmp_path)], emitter=emitter
            )
    finally:
        _cleanup_isolated_schema(conn, schema)
    emitter.flush()

    (entry,) = read_lines(log_path)
    assert entry["outcome"] == "failure"
    assert entry["applied"] == []
    assert entry["already_applied"] == 0
    assert "1/0" not in str(entry)


@pytest.mark.postgres_live
def test_audit_events_applies_cleanly_and_creates_the_expected_table() -> None:
    """`audit_events` round-trips an ordinary insert (issue #147)."""
    _require_configured_postgres()
    conn, schema = _isolated_connection(load_config())
    try:
        apply_pending_migrations(conn, sources=STATE_MIGRATION_SOURCES)
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO audit_events "
                "(actor_subject, actor_issuer, action, resource_type, resource_id, outcome, "
                "details) VALUES "
                "('test-actor', 'https://issuer.example.com/', 'test.action', 'principal', "
                "'migration-test-target', 'applied', '{\"key\": \"value\"}'::jsonb)"
            )
        conn.commit()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT actor_subject, actor_issuer, action, resource_type, resource_id, "
                "outcome, details FROM audit_events"
            )
            row = cur.fetchone()
    finally:
        _cleanup_isolated_schema(conn, schema)

    assert row == (
        "test-actor",
        "https://issuer.example.com/",
        "test.action",
        "principal",
        "migration-test-target",
        "applied",
        {"key": "value"},
    )


@pytest.mark.postgres_live
def test_audit_events_outcome_check_constraint_rejects_an_unknown_outcome() -> None:
    """`audit_events.outcome`'s `CHECK` constraint admits only `applied`/`rejected`/`failed`."""
    _require_configured_postgres()
    conn, schema = _isolated_connection(load_config())
    try:
        apply_pending_migrations(conn, sources=STATE_MIGRATION_SOURCES)
        with (
            pytest.raises(psycopg.errors.CheckViolation),
            conn.cursor() as cur,
        ):
            cur.execute(
                "INSERT INTO audit_events "
                "(actor_subject, actor_issuer, action, resource_type, resource_id, outcome) "
                "VALUES ('a', 'b', 'c', 'd', 'e', 'not_a_real_outcome')"
            )
        conn.rollback()
    finally:
        _cleanup_isolated_schema(conn, schema)
