"""Fast tests for the fail-closed startup verifier's error and log shape (issue #205 slice 8).

The verifier's database behavior (tracking rows, table existence, owner membership) is proven
against a real Postgres by `tests/graph_gateway/test_startup_check_live.py`.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Self, cast

import pytest

from ps_service.graph_gateway import GRAPH_LOG_TABLES
from ps_service.main import GRAPH_GATEWAY_MIGRATION_SOURCE, startup_failure_log_extra
from ps_service.persistence import GraphLogMigrationMissingError, StatePostgresProvisioningError
from ps_service.persistence.migration_runner import split_statements
from ps_service.persistence.privileged_migration_runner import (
    render_migration_sql,
    verify_privileged_migrations_applied,
)

if TYPE_CHECKING:
    import psycopg
    from psycopg.rows import TupleRow

_MIGRATION = "graph_gateway/0001_graph_mutation_log.sql"


def test_missing_migration_error_message_names_component_and_filename_only() -> None:
    error = GraphLogMigrationMissingError(_MIGRATION, reason="migration_not_recorded")

    message = str(error)
    assert _MIGRATION in message
    assert "python -m ps_service.graph_gateway.provision" in message
    assert error.missing_migration == _MIGRATION
    assert error.reason == "migration_not_recorded"
    for forbidden in ("host", "password", "SELECT", "psycopg", "postgresql://"):
        assert forbidden not in message


def test_missing_migration_error_is_a_provisioning_error() -> None:
    assert issubclass(GraphLogMigrationMissingError, StatePostgresProvisioningError)


def test_startup_failure_log_extra_carries_missing_migration_and_reason_class() -> None:
    error = GraphLogMigrationMissingError(_MIGRATION, reason="table_missing")

    extra = startup_failure_log_extra(error)

    assert extra == {
        "dependency": "state_postgres",
        "reason": "GraphLogMigrationMissingError",
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

    with pytest.raises(GraphLogMigrationMissingError) as raised:
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
