"""Fast tests for the fail-closed startup verifier's error and log shape (issue #205 slice 8).

The verifier's database behavior (tracking rows, table existence, owner membership) is proven
against a real Postgres by `tests/graph_gateway/test_startup_check_live.py`.
"""

from __future__ import annotations

from ps_service.main import startup_failure_log_extra
from ps_service.persistence import GraphLogMigrationMissingError, StatePostgresProvisioningError

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
