"""Fast tests for the fail-closed startup verifier's error and log shape.

The verifier's database behavior (tracking rows, table existence, owner membership) is proven
against a real Postgres by `tests/graph_gateway/test_startup_check_live.py`.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Self, cast, get_args

import pytest

from ps_service.audit import AUDIT_LOG_SCHEMAS, AUDIT_LOG_TABLES
from ps_service.graph_gateway import GRAPH_LOG_TABLES
from ps_service.main import (
    GRAPH_GATEWAY_MIGRATION_SOURCE,
    _verify_privileged_migrations_at_startup,  # pyright: ignore[reportPrivateUsage]  # wiring under test
    startup_failure_log_extra,
)
from ps_service.persistence import PrivilegedMigrationMissingError, StatePostgresProvisioningError
from ps_service.persistence.errors import MissingMigrationReason
from ps_service.persistence.migration_runner import split_statements
from ps_service.persistence.privileged_migration_runner import (
    render_migration_sql,
    verify_privileged_migrations_applied,
)
from ps_service.state_migrations import AUDIT_INTEGRITY_MIGRATION_SOURCE

if TYPE_CHECKING:
    import psycopg
    from psycopg.rows import TupleRow

_MIGRATION = "graph_gateway/0001_graph_mutation_log.sql"
_AUDIT_MIGRATION = "audit_integrity/0001_audit_log_insert_only_hash_chain.sql"


def test_missing_migration_error_message_names_component_and_filename_only() -> None:
    error = PrivilegedMigrationMissingError(_MIGRATION, reason="migration_not_recorded")

    message = str(error)
    assert _MIGRATION in message
    assert "python -m ps_service.graph_gateway.provision" in message
    assert error.missing_migration == _MIGRATION
    assert error.reason == "migration_not_recorded"
    for forbidden in ("host", "password", "SELECT", "psycopg", "postgresql://"):
        assert forbidden not in message


def test_missing_migration_error_is_a_provisioning_error() -> None:
    assert issubclass(PrivilegedMigrationMissingError, StatePostgresProvisioningError)


def test_startup_failure_log_extra_carries_missing_migration_and_reason_class() -> None:
    error = PrivilegedMigrationMissingError(_MIGRATION, reason="table_missing")

    extra = startup_failure_log_extra(error)

    assert extra == {
        "dependency": "state_postgres",
        "reason": "PrivilegedMigrationMissingError",
        "missing_migration": _MIGRATION,
        "missing_reason": "table_missing",
    }


def test_startup_failure_log_extra_for_any_other_error_has_class_name_only() -> None:
    extra = startup_failure_log_extra(ConnectionError("could not connect to db.internal:5432"))

    assert extra == {"dependency": "state_postgres", "reason": "ConnectionError"}


class _TrackingCursor:
    """A cursor that answers only the tracking-table questions of the verifier."""

    def __init__(self, recorded: set[str]) -> None:
        self._recorded = recorded
        self._answer: tuple[object, ...] | None = None

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def execute(self, query: str, params: dict[str, str] | None = None) -> None:
        del query
        filename = None if params is None else params.get("filename")
        self._answer = (
            (True,) if filename is None else ((1,) if filename in self._recorded else None)
        )

    def fetchone(self) -> tuple[object, ...] | None:
        return self._answer


class _TrackingConnection:
    def __init__(self, recorded: set[str]) -> None:
        self._recorded = recorded

    def cursor(self) -> _TrackingCursor:
        return _TrackingCursor(self._recorded)


def test_a_database_with_only_the_first_migration_recorded_fails_closed_naming_the_second() -> None:
    conn = cast(
        "psycopg.Connection[TupleRow]", _TrackingConnection({"0001_graph_mutation_log.sql"})
    )

    with pytest.raises(PrivilegedMigrationMissingError) as raised:
        verify_privileged_migrations_applied(
            conn, sources=[GRAPH_GATEWAY_MIGRATION_SOURCE], required_tables=GRAPH_LOG_TABLES
        )

    assert raised.value.missing_migration == "graph_gateway/0002_group_swap_set.sql"
    assert raised.value.reason == "migration_not_recorded"


def test_the_swap_set_migration_renders_to_four_statements_run_as_the_owner_role() -> None:
    path = next(file for file in GRAPH_GATEWAY_MIGRATION_SOURCE.directory.glob("0002_*.sql"))

    rendered = render_migration_sql(
        path.read_text(encoding="utf-8"), owner_role="ps_state_graph_owner", app_role="ps_state"
    )

    statements = split_statements(re.sub(r"--[^\n]*\n", "", rendered))
    assert [statement.strip() for statement in statements] == [
        'SET LOCAL ROLE "ps_state_graph_owner"',
        "ALTER TABLE graph_log.groups ADD COLUMN swap_set_id uuid",
        (
            "CREATE INDEX groups_swap_set_idx ON graph_log.groups (swap_set_id) "
            "WHERE swap_set_id IS NOT NULL"
        ),
        "RESET ROLE",
    ]


def test_the_reason_vocabulary_includes_a_schema_controlled_by_the_state_role() -> None:
    assert "state_role_controls_schema" in get_args(MissingMigrationReason)


def test_the_audit_error_message_names_the_file_and_the_provisioning_command_only() -> None:
    error = PrivilegedMigrationMissingError(_AUDIT_MIGRATION, reason="state_role_controls_schema")

    message = str(error)
    assert _AUDIT_MIGRATION in message
    assert "python -m ps_service.graph_gateway.provision" in message
    assert "graph log" not in message
    for forbidden in ("host", "password", "SELECT", "psycopg", "postgresql://"):
        assert forbidden not in message


def test_the_audit_source_and_objects_are_exported_for_the_verifier() -> None:
    assert AUDIT_INTEGRITY_MIGRATION_SOURCE.component == "audit_integrity"
    assert AUDIT_LOG_TABLES == ("audit_log.audit_events", "audit_log.audit_chain_head")
    assert AUDIT_LOG_SCHEMAS == ("audit_log",)


def test_the_old_graph_only_error_name_no_longer_exists() -> None:
    from ps_service import persistence  # asserts the absence of a name

    assert not hasattr(persistence, "GraphLogMigrationMissingError")


class _CatalogCursor:
    """A cursor answering the verifier's tracking, table-control and schema-control queries."""

    def __init__(
        self,
        recorded: set[str],
        *,
        controlled_schemas: frozenset[str],
        missing_tables: frozenset[str],
    ) -> None:
        self._recorded = recorded
        self._controlled_schemas = controlled_schemas
        self._missing_tables = missing_tables
        self._answer: tuple[object, ...] | None = None

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def execute(self, query: str, params: dict[str, str] | None = None) -> None:
        params = params or {}
        if "filename" in params:
            self._answer = (1,) if params["filename"] in self._recorded else None
        elif "pg_namespace" in query:
            name = params["name"]
            self._answer = (name in self._controlled_schemas,)
        elif "pg_has_role" in query:
            self._answer = None if params["name"] in self._missing_tables else (False,)
        else:
            self._answer = (True,)

    def fetchone(self) -> tuple[object, ...] | None:
        return self._answer


class _CatalogConnection:
    def __init__(
        self,
        recorded: set[str],
        *,
        controlled_schemas: frozenset[str] = frozenset(),
        missing_tables: frozenset[str] = frozenset(),
    ) -> None:
        self._args = (recorded, controlled_schemas, missing_tables)

    def cursor(self) -> _CatalogCursor:
        recorded, controlled_schemas, missing_tables = self._args
        return _CatalogCursor(
            recorded, controlled_schemas=controlled_schemas, missing_tables=missing_tables
        )


_GRAPH_RECORDED = {"0001_graph_mutation_log.sql", "0002_group_swap_set.sql"}


def test_startup_names_the_audit_migration_when_only_the_graph_migration_is_recorded() -> None:
    conn = cast("psycopg.Connection[TupleRow]", _CatalogConnection(_GRAPH_RECORDED))

    with pytest.raises(PrivilegedMigrationMissingError) as raised:
        _verify_privileged_migrations_at_startup(conn)

    assert raised.value.missing_migration == _AUDIT_MIGRATION
    assert raised.value.reason == "migration_not_recorded"
    assert startup_failure_log_extra(raised.value) == {
        "dependency": "state_postgres",
        "reason": "PrivilegedMigrationMissingError",
        "missing_migration": _AUDIT_MIGRATION,
        "missing_reason": "migration_not_recorded",
    }


def test_startup_names_the_audit_migration_when_the_state_role_owns_the_audit_schema() -> None:
    conn = cast(
        "psycopg.Connection[TupleRow]",
        _CatalogConnection(
            _GRAPH_RECORDED | {"0001_audit_log_insert_only_hash_chain.sql"},
            controlled_schemas=frozenset({"audit_log"}),
        ),
    )

    with pytest.raises(PrivilegedMigrationMissingError) as raised:
        _verify_privileged_migrations_at_startup(conn)

    assert raised.value.missing_migration == _AUDIT_MIGRATION
    assert raised.value.reason == "state_role_controls_schema"


def test_startup_names_the_audit_migration_when_the_audit_table_is_missing() -> None:
    conn = cast(
        "psycopg.Connection[TupleRow]",
        _CatalogConnection(
            _GRAPH_RECORDED | {"0001_audit_log_insert_only_hash_chain.sql"},
            missing_tables=frozenset({"audit_log.audit_events"}),
        ),
    )

    with pytest.raises(PrivilegedMigrationMissingError) as raised:
        _verify_privileged_migrations_at_startup(conn)

    assert (raised.value.missing_migration, raised.value.reason) == (
        _AUDIT_MIGRATION,
        "table_missing",
    )


def test_startup_passes_when_both_privileged_migrations_are_in_place() -> None:
    conn = cast(
        "psycopg.Connection[TupleRow]",
        _CatalogConnection(_GRAPH_RECORDED | {"0001_audit_log_insert_only_hash_chain.sql"}),
    )

    _verify_privileged_migrations_at_startup(conn)
