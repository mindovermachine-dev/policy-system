"""`postgres_live` tests for `apply_pending_migrations(run_as_role=...)` (issue #205 follow-up).

The provisioning command connects with the ADMIN credential but must leave every public object
owned by the `ps_state` application role, exactly as service startup does. The runner therefore
switches role per transaction (`SET LOCAL ROLE`); the default (`None`) keeps the connecting
role's ownership. Real Postgres, real init-script roles; nothing mocked.

Deselected by default -- run with `uv run pytest -m postgres_live`.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

import psycopg
import pytest
from psycopg import sql

from ps_service.persistence import (
    MigrationSource,
    StatePostgresProvisioningError,
    apply_pending_migrations,
)

if TYPE_CHECKING:
    from pathlib import Path

    from psycopg.rows import TupleRow

    from persistence.provisioned_postgres import Provisioned

pytestmark = pytest.mark.postgres_live


def _source(tmp_path: Path) -> tuple[MigrationSource, str]:
    table = f"owned_t_{uuid.uuid4().hex[:10]}"
    directory = tmp_path / "migrations"
    directory.mkdir()
    (directory / "0001_create.sql").write_text(
        f"CREATE TABLE {table} (id int PRIMARY KEY); CREATE INDEX {table}_idx ON {table} (id)",
        encoding="utf-8",
    )
    return MigrationSource(f"own_{uuid.uuid4().hex[:8]}", directory), table


def _admin(prov: Provisioned, user: str | None = None) -> psycopg.Connection[TupleRow]:
    return psycopg.connect(
        host=prov.host, port=prov.port, user=user or prov.superuser, dbname=prov.state_db
    )


def _owners(prov: Provisioned, table: str) -> dict[str, object]:
    with prov.superuser_connect(prov.state_db) as conn:
        rows = conn.execute(
            "SELECT relname, pg_get_userbyid(relowner) FROM pg_class "
            "WHERE relname IN (%s, %s, 'ps_schema_migrations')",
            (table, f"{table}_idx"),
        ).fetchall()
    return {str(row[0]): row[1] for row in rows}


def test_run_as_role_makes_that_role_own_the_created_objects_and_the_tracking_table(
    fresh_provisioned: Provisioned, tmp_path: Path
) -> None:
    source, table = _source(tmp_path)

    with _admin(fresh_provisioned) as admin:
        applied = apply_pending_migrations(
            admin, sources=[source], run_as_role=fresh_provisioned.state_user
        )
        current = admin.execute("SELECT current_user").fetchone()

    assert applied == ["0001_create.sql"]
    assert _owners(fresh_provisioned, table) == {
        table: fresh_provisioned.state_user,
        f"{table}_idx": fresh_provisioned.state_user,
        "ps_schema_migrations": fresh_provisioned.state_user,
    }
    assert current == (fresh_provisioned.superuser,)  # SET LOCAL did not leak past the txn


def test_run_as_role_default_none_keeps_the_connecting_roles_ownership(
    fresh_provisioned: Provisioned, tmp_path: Path
) -> None:
    source, table = _source(tmp_path)

    with _admin(fresh_provisioned) as admin:
        apply_pending_migrations(admin, sources=[source])

    assert set(_owners(fresh_provisioned, table).values()) == {fresh_provisioned.superuser}


def test_run_as_role_second_run_applies_nothing(
    fresh_provisioned: Provisioned, tmp_path: Path
) -> None:
    source, _ = _source(tmp_path)
    with _admin(fresh_provisioned) as admin:
        apply_pending_migrations(admin, sources=[source], run_as_role=fresh_provisioned.state_user)

        second = apply_pending_migrations(
            admin, sources=[source], run_as_role=fresh_provisioned.state_user
        )

    assert second == []


def test_run_as_role_names_the_role_when_the_connection_cannot_switch_to_it(
    fresh_provisioned: Provisioned, tmp_path: Path
) -> None:
    source, table = _source(tmp_path)
    admin_role = f"nomember_{uuid.uuid4().hex[:8]}"
    with fresh_provisioned.superuser_connect() as conn:
        conn.execute(sql.SQL("CREATE ROLE {} LOGIN NOSUPERUSER").format(sql.Identifier(admin_role)))
        conn.execute(
            sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                sql.Identifier(fresh_provisioned.state_db), sql.Identifier(admin_role)
            )
        )
    try:
        with _admin(fresh_provisioned, admin_role) as admin:
            with pytest.raises(StatePostgresProvisioningError) as raised:
                apply_pending_migrations(
                    admin, sources=[source], run_as_role=fresh_provisioned.state_user
                )
            usable = admin.execute("SELECT 1").fetchone()
    finally:
        with fresh_provisioned.superuser_connect() as conn:
            conn.execute(
                sql.SQL("REVOKE CONNECT ON DATABASE {} FROM {}").format(
                    sql.Identifier(fresh_provisioned.state_db), sql.Identifier(admin_role)
                )
            )
            conn.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(admin_role)))

    assert str(raised.value) == f"admin connection cannot SET ROLE {fresh_provisioned.state_user}"
    assert usable == (1,)  # the failed transaction was rolled back
    assert table not in _owners(fresh_provisioned, table)
