"""`postgres_live` tests for the startup verification of the audit integrity migration.

Connects as the real `ps_state` role (never an admin credential) to scratch clusters built by the
Helm init script. The verifier must fail closed, naming `audit_integrity/0001_...sql`, when the
migration is unrecorded, a table or the schema is missing, or `ps_state` controls a table or the
schema. The graph log half of the same check lives in
`tests/graph_gateway/test_startup_check_live.py` and the lifespan wiring in `tests/test_main.py`.

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

from ps_service.audit import AUDIT_LOG_SCHEMAS, AUDIT_LOG_TABLES
from ps_service.main import (
    _verify_privileged_migrations_at_startup,  # pyright: ignore[reportPrivateUsage]  # the wiring under test
)
from ps_service.persistence import PrivilegedMigrationMissingError, connect_from_config
from ps_service.persistence.privileged_migration_runner import verify_privileged_migrations_applied
from ps_service.state_migrations import AUDIT_INTEGRITY_MIGRATION_SOURCE

if TYPE_CHECKING:
    from pathlib import Path

    from ps_service.logging import LogEmitter

pytestmark = pytest.mark.postgres_live

_AUDIT_FILE = "audit_integrity/0001_audit_log_insert_only_hash_chain.sql"


class MakeEmitter(Protocol):
    """Call shape of the shared `make_emitter` fixture (`tests/conftest.py`)."""

    def __call__(self) -> tuple[LogEmitter, Path]: ...


class ReadLines(Protocol):
    """Call shape of the shared `read_lines` fixture (`tests/conftest.py`)."""

    def __call__(self, log_path: Path) -> list[dict[str, object]]: ...


def _verify_as_state_role(prov: Provisioned) -> None:
    with connect_from_config(prov.state_config()) as conn:
        _verify_privileged_migrations_at_startup(conn)


def _admin_execute(prov: Provisioned, statement: sql.SQL | sql.Composed) -> None:
    with prov.superuser_connect(prov.state_db) as admin:
        admin.execute(statement)


def _expect_failure(prov: Provisioned, reason: str) -> PrivilegedMigrationMissingError:
    with pytest.raises(PrivilegedMigrationMissingError) as raised:
        _verify_as_state_role(prov)
    assert raised.value.missing_migration == _AUDIT_FILE
    assert raised.value.reason == reason
    assert _AUDIT_FILE in str(raised.value)
    assert prov.state_db not in str(raised.value)
    return raised.value


def test_both_verifications_pass_with_only_state_credentials_after_provisioning(
    fresh_provisioned: Provisioned,
) -> None:
    provision_graph_log(fresh_provisioned)

    _verify_as_state_role(fresh_provisioned)


def test_a_database_with_only_the_ordinary_migrations_fails_naming_the_graph_file_first(
    fresh_provisioned: Provisioned,
) -> None:
    migrate_state_database(fresh_provisioned)

    with pytest.raises(PrivilegedMigrationMissingError) as raised:
        _verify_as_state_role(fresh_provisioned)

    assert raised.value.missing_migration == "graph_gateway/0001_graph_mutation_log.sql"


def test_an_unrecorded_audit_migration_fails_closed_naming_the_audit_file(
    fresh_provisioned: Provisioned,
) -> None:
    provision_graph_log(fresh_provisioned)
    _admin_execute(
        fresh_provisioned,
        sql.SQL("DELETE FROM ps_schema_migrations WHERE component = 'audit_integrity'"),
    )

    _expect_failure(fresh_provisioned, "migration_not_recorded")


def test_a_dropped_audit_table_fails_closed_as_table_missing(
    fresh_provisioned: Provisioned,
) -> None:
    provision_graph_log(fresh_provisioned)
    _admin_execute(fresh_provisioned, sql.SQL("DROP TABLE audit_log.audit_events CASCADE"))

    _expect_failure(fresh_provisioned, "table_missing")


def test_a_dropped_chain_head_table_fails_closed_as_table_missing(
    fresh_provisioned: Provisioned,
) -> None:
    provision_graph_log(fresh_provisioned)
    _admin_execute(fresh_provisioned, sql.SQL("DROP TABLE audit_log.audit_chain_head CASCADE"))

    _expect_failure(fresh_provisioned, "table_missing")


def test_an_audit_table_owned_by_the_state_role_fails_closed(
    fresh_provisioned: Provisioned,
) -> None:
    provision_graph_log(fresh_provisioned)
    _admin_execute(
        fresh_provisioned,
        sql.SQL("ALTER TABLE audit_log.audit_events OWNER TO {}").format(
            sql.Identifier(fresh_provisioned.state_user)
        ),
    )

    _expect_failure(fresh_provisioned, "state_role_controls_table")


def test_an_audit_schema_owned_by_the_state_role_fails_closed(
    fresh_provisioned: Provisioned,
) -> None:
    provision_graph_log(fresh_provisioned)
    _admin_execute(
        fresh_provisioned,
        sql.SQL("ALTER SCHEMA audit_log OWNER TO {}").format(
            sql.Identifier(fresh_provisioned.state_user)
        ),
    )

    _expect_failure(fresh_provisioned, "state_role_controls_schema")


def test_a_state_role_in_the_owner_role_fails_closed(fresh_provisioned: Provisioned) -> None:
    provision_graph_log(fresh_provisioned)
    with fresh_provisioned.superuser_connect() as admin:
        admin.execute(
            sql.SQL("GRANT {} TO {}").format(
                sql.Identifier(fresh_provisioned.owner_role),
                sql.Identifier(fresh_provisioned.state_user),
            )
        )

    with pytest.raises(PrivilegedMigrationMissingError) as raised:
        _verify_as_state_role(fresh_provisioned)

    assert raised.value.reason in {"state_role_controls_table", "state_role_controls_schema"}


def test_a_passing_check_changes_nothing(fresh_provisioned: Provisioned) -> None:
    provision_graph_log(fresh_provisioned)

    def snapshot() -> tuple[object, ...]:
        with fresh_provisioned.superuser_connect(fresh_provisioned.state_db) as admin:
            tracked = admin.execute(
                "SELECT component, filename, applied_at FROM ps_schema_migrations ORDER BY 1, 2"
            ).fetchall()
            tables = admin.execute(
                "SELECT schemaname, tablename, tableowner FROM pg_tables "
                "WHERE schemaname IN ('public', 'graph_log', 'audit_log') ORDER BY 1, 2"
            ).fetchall()
            events = admin.execute("SELECT count(*) FROM audit_log.audit_events").fetchone()
        return (tracked, tables, events)

    before = snapshot()
    _verify_as_state_role(fresh_provisioned)

    assert snapshot() == before


def _verify_audit_source(prov: Provisioned, emitter: LogEmitter) -> None:
    with connect_from_config(prov.state_config()) as conn:
        verify_privileged_migrations_applied(
            conn,
            sources=[AUDIT_INTEGRITY_MIGRATION_SOURCE],
            required_tables=AUDIT_LOG_TABLES,
            required_schemas=AUDIT_LOG_SCHEMAS,
            emitter=emitter,
        )


def _verify_entries(
    emitter: LogEmitter, log_path: Path, read_lines: ReadLines
) -> list[dict[str, object]]:
    emitter.flush()
    return [
        line
        for line in read_lines(log_path)
        if line.get("action") == "verify_privileged_migrations"
    ]


def test_a_passing_audit_check_emits_one_success_entry(
    fresh_provisioned: Provisioned, make_emitter: MakeEmitter, read_lines: ReadLines
) -> None:
    emitter, log_path = make_emitter()
    provision_graph_log(fresh_provisioned)

    _verify_audit_source(fresh_provisioned, emitter)

    entries = _verify_entries(emitter, log_path, read_lines)
    assert [entry["outcome"] for entry in entries] == ["success"]


def test_a_failing_audit_check_emits_one_failure_entry_with_the_file_and_reason_only(
    fresh_provisioned: Provisioned, make_emitter: MakeEmitter, read_lines: ReadLines
) -> None:
    emitter, log_path = make_emitter()
    provision_graph_log(fresh_provisioned)
    _admin_execute(fresh_provisioned, sql.SQL("DROP TABLE audit_log.audit_events CASCADE"))

    with pytest.raises(PrivilegedMigrationMissingError):
        _verify_audit_source(fresh_provisioned, emitter)

    entries = _verify_entries(emitter, log_path, read_lines)
    assert len(entries) == 1
    assert entries[0]["outcome"] == "failure"
    assert entries[0]["missing_migration"] == _AUDIT_FILE
    assert entries[0]["reason"] == "table_missing"
    assert fresh_provisioned.state_db not in str(entries[0])
    assert fresh_provisioned.state_password not in str(entries[0])
