"""`postgres_live` tests for the fail-closed startup verifier (issue #205 slice 8, AC-BI-012).

Connects as the real `ps_state` role (never an admin credential) to scratch clusters built by the
Helm init script, and proves `verify_privileged_migrations_applied` passes only when the
`graph_gateway` migration is recorded, every immutable table exists, and `ps_state` neither owns
a table nor is a member of an owner role. The lifespan wiring is covered by `tests/test_main.py`.

Deselected by default -- run with `uv run pytest -m postgres_live`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

import pytest
from persistence.provisioned_postgres import (
    Provisioned,
    migrate_state_database,
    provision_graph_log,
)
from psycopg import sql

from ps_service.graph_gateway import GRAPH_LOG_TABLES, MIGRATIONS_DIR
from ps_service.persistence import (
    GraphLogMigrationMissingError,
    MigrationSource,
    connect_from_config,
)
from ps_service.persistence.privileged_migration_runner import (
    verify_privileged_migrations_applied,
)

if TYPE_CHECKING:
    from pathlib import Path

    from ps_service.logging import LogEmitter

pytestmark = pytest.mark.postgres_live

_VERIFY_ACTION = "verify_privileged_migrations"
_MIGRATION = "graph_gateway/0001_graph_mutation_log.sql"
_SOURCES = [MigrationSource("graph_gateway", MIGRATIONS_DIR)]


class MakeEmitter(Protocol):
    """Call shape of the shared `make_emitter` fixture (`tests/conftest.py`)."""

    def __call__(self) -> tuple[LogEmitter, Path]: ...


class ReadLines(Protocol):
    """Call shape of the shared `read_lines` fixture (`tests/conftest.py`)."""

    def __call__(self, log_path: Path) -> list[dict[str, object]]: ...


def _verify_as_state_role(prov: Provisioned, emitter: LogEmitter | None = None) -> None:
    with connect_from_config(prov.state_config()) as conn:
        verify_privileged_migrations_applied(
            conn, sources=_SOURCES, required_tables=GRAPH_LOG_TABLES, emitter=emitter
        )


def _provisioned_cluster(prov: Provisioned) -> Provisioned:
    migrate_state_database(prov)
    provision_graph_log(prov)
    return prov


def _graph_log_schema_exists(prov: Provisioned) -> bool:
    with prov.superuser_connect(prov.state_db) as conn:
        row = conn.execute(
            "SELECT count(*) FROM pg_namespace WHERE nspname = 'graph_log'"
        ).fetchone()
    assert row is not None
    return row[0] == 1


def test_startup_check_passes_with_only_state_credentials_after_provisioning(
    fresh_provisioned: Provisioned,
) -> None:
    _provisioned_cluster(fresh_provisioned)

    _verify_as_state_role(fresh_provisioned)


def test_startup_check_fails_closed_naming_graph_gateway_0001_when_the_migration_is_unrecorded(
    fresh_provisioned: Provisioned,
) -> None:
    migrate_state_database(fresh_provisioned)

    with pytest.raises(GraphLogMigrationMissingError) as raised:
        _verify_as_state_role(fresh_provisioned)

    assert raised.value.missing_migration == _MIGRATION
    assert raised.value.reason == "migration_not_recorded"
    assert _MIGRATION in str(raised.value)
    assert "python -m ps_service.graph_gateway.provision" in str(raised.value)
    assert fresh_provisioned.state_db not in str(raised.value)


def test_startup_check_never_creates_the_immutable_tables_itself(
    fresh_provisioned: Provisioned,
) -> None:
    migrate_state_database(fresh_provisioned)

    with pytest.raises(GraphLogMigrationMissingError):
        _verify_as_state_role(fresh_provisioned)

    assert not _graph_log_schema_exists(fresh_provisioned)


def test_startup_check_fails_closed_when_the_tracking_table_does_not_exist_yet(
    fresh_provisioned: Provisioned,
) -> None:
    with pytest.raises(GraphLogMigrationMissingError) as raised:
        _verify_as_state_role(fresh_provisioned)

    assert raised.value.missing_migration == _MIGRATION
    assert raised.value.reason == "migration_not_recorded"


def test_startup_check_fails_closed_when_tracking_row_exists_but_a_table_was_dropped(
    fresh_provisioned: Provisioned,
) -> None:
    _provisioned_cluster(fresh_provisioned)
    with fresh_provisioned.superuser_connect(fresh_provisioned.state_db) as admin:
        admin.execute("DROP TABLE graph_log.checkpoints")

    with pytest.raises(GraphLogMigrationMissingError) as raised:
        _verify_as_state_role(fresh_provisioned)

    assert raised.value.missing_migration == _MIGRATION
    assert raised.value.reason == "table_missing"


def test_startup_check_fails_closed_when_the_state_role_owns_an_immutable_table(
    fresh_provisioned: Provisioned,
) -> None:
    _provisioned_cluster(fresh_provisioned)
    with fresh_provisioned.superuser_connect(fresh_provisioned.state_db) as admin:
        admin.execute(
            sql.SQL("ALTER TABLE graph_log.entries OWNER TO {}").format(
                sql.Identifier(fresh_provisioned.state_user)
            )
        )

    with pytest.raises(GraphLogMigrationMissingError) as raised:
        _verify_as_state_role(fresh_provisioned)

    assert raised.value.reason == "state_role_controls_table"


def test_startup_check_fails_closed_when_the_state_role_is_a_member_of_the_owner_role(
    fresh_provisioned: Provisioned,
) -> None:
    _provisioned_cluster(fresh_provisioned)
    with fresh_provisioned.superuser_connect() as admin:
        admin.execute(
            sql.SQL("GRANT {} TO {}").format(
                sql.Identifier(fresh_provisioned.owner_role),
                sql.Identifier(fresh_provisioned.state_user),
            )
        )

    with pytest.raises(GraphLogMigrationMissingError) as raised:
        _verify_as_state_role(fresh_provisioned)

    assert raised.value.reason == "state_role_controls_table"


def test_startup_check_changes_nothing_when_it_passes(fresh_provisioned: Provisioned) -> None:
    _provisioned_cluster(fresh_provisioned)

    def snapshot() -> tuple[object, ...]:
        with fresh_provisioned.superuser_connect(fresh_provisioned.state_db) as admin:
            tracked = admin.execute(
                "SELECT component, filename FROM ps_schema_migrations ORDER BY 1, 2"
            ).fetchall()
            tables = admin.execute(
                "SELECT schemaname, tablename, tableowner FROM pg_tables "
                "WHERE schemaname IN ('public', 'graph_log') ORDER BY 1, 2"
            ).fetchall()
        return (tracked, tables)

    before = snapshot()
    _verify_as_state_role(fresh_provisioned)

    assert snapshot() == before


def test_startup_check_emits_one_failure_entry_naming_the_migration_and_no_connection_detail(
    fresh_provisioned: Provisioned, make_emitter: MakeEmitter, read_lines: ReadLines
) -> None:
    emitter, log_path = make_emitter()
    migrate_state_database(fresh_provisioned)

    with pytest.raises(GraphLogMigrationMissingError):
        _verify_as_state_role(fresh_provisioned, emitter)
    emitter.flush()

    entries = [
        line
        for line in read_lines(log_path)
        if line.get("action") == "verify_privileged_migrations"
    ]
    assert len(entries) == 1
    assert (entries[0]["component"], entries[0]["outcome"]) == ("persistence", "failure")
    assert entries[0]["missing_migration"] == _MIGRATION
    assert entries[0]["reason"] == "migration_not_recorded"
    assert fresh_provisioned.state_db not in str(entries[0])
    assert fresh_provisioned.state_password not in str(entries[0])


def test_startup_check_emits_one_success_entry_when_everything_is_in_place(
    fresh_provisioned: Provisioned, make_emitter: MakeEmitter, read_lines: ReadLines
) -> None:
    emitter, log_path = make_emitter()
    _provisioned_cluster(fresh_provisioned)

    _verify_as_state_role(fresh_provisioned, emitter)
    emitter.flush()

    entries = [
        line
        for line in read_lines(log_path)
        if line.get("action") == "verify_privileged_migrations"
    ]
    assert len(entries) == 1
    assert entries[0]["outcome"] == "success"
