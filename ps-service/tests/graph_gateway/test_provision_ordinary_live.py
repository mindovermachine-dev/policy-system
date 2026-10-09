"""`postgres_live` tests: the provisioning CLI applies the ordinary ps_state migrations itself.

Issue #205 follow-up. On an empty `ps_state` the CLI (admin credential) runs the ordinary
component migrations as the `ps_state` application role (`SET LOCAL ROLE`), so `ps_state` owns
every public object exactly as service startup would have made it, and then creates the
owner-protected `graph_log` tables. Every test drives the real `main` entry point against a real
Postgres provisioned by the real Helm init script; nothing is mocked.

Deselected by default -- run with `uv run pytest -m postgres_live` (see
`persistence/provisioned_postgres.py`).
"""

from __future__ import annotations

import threading
import uuid
from typing import TYPE_CHECKING

import psycopg
import pytest
from persistence.provisioned_postgres import (
    Provisioned,
    create_provisioned,
    drop_cluster_objects,
    migrate_state_database,
)
from psycopg import sql

from ps_service.graph_gateway.provision import ProvisionResult, main, provision
from ps_service.persistence import apply_pending_migrations, connect_from_config
from ps_service.state_migrations import ORDINARY_STATE_MIGRATION_SOURCES

if TYPE_CHECKING:
    from collections.abc import Iterator

pytestmark = pytest.mark.postgres_live

_ORDINARY_FILES = [
    ("audit", "0001_audit_events.sql"),
    ("audit", "0002_audit_events_details_indexes.sql"),
    ("authz", "0001_access_role_assignments.sql"),
    ("runtime_config", "0001_runtime_config.sql"),
    ("ingestion_runs", "0001_ingestion_runs.sql"),
]
_GRAPH_LOG_TABLES = ["applied_markers", "checkpoints", "entries", "groups", "payloads"]
ADMIN_PASSWORD = "admin-pw-" + uuid.uuid4().hex


def cli_environ(prov: Provisioned, *, admin_user: str | None = None) -> dict[str, str]:
    """Return the environment the CLI reads, with the scratch superuser as admin by default."""
    return {
        "PS_STATE_POSTGRES_HOST": prov.host,
        "PS_STATE_POSTGRES_PORT": str(prov.port),
        "PS_STATE_POSTGRES_DATABASE": prov.state_db,
        "PS_STATE_POSTGRES_USER": prov.state_user,
        "PS_STATE_ADMIN_POSTGRES_USER": admin_user or prov.superuser,
        "PS_STATE_ADMIN_POSTGRES_PASSWORD": ADMIN_PASSWORD,
        "PS_STATE_GRAPH_OWNER_ROLE": prov.owner_role,
    }


def scalar(conn: psycopg.Connection[tuple[object, ...]], query: str, *params: object) -> object:
    row = conn.execute(query, params).fetchone()  # pyright: ignore[reportArgumentType]  # test-only dynamic SQL
    assert row is not None
    return row[0]


def _tracking_rows(prov: Provisioned) -> list[tuple[str, str]]:
    with prov.as_state() as conn:
        rows = conn.execute(
            "SELECT component, filename FROM ps_schema_migrations ORDER BY component, filename"
        ).fetchall()
    return [(str(row[0]), str(row[1])) for row in rows]


def _graph_log_tables(prov: Provisioned) -> list[str]:
    with prov.as_state() as conn:
        rows = conn.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'graph_log' ORDER BY 1"
        ).fetchall()
    return [str(row[0]) for row in rows]


def test_cli_on_empty_ps_state_applies_ordinary_then_graph_gateway_and_exits_zero(
    fresh_provisioned: Provisioned, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = main([], cli_environ(fresh_provisioned))

    captured = capsys.readouterr()
    assert exit_code == 0, captured.err
    assert _tracking_rows(fresh_provisioned) == sorted(
        [*_ORDINARY_FILES, ("graph_gateway", "0001_graph_mutation_log.sql")]
    )
    assert _graph_log_tables(fresh_provisioned) == _GRAPH_LOG_TABLES
    assert "applied 5 ordinary + 1 graph_gateway migration(s)" in captured.out
    assert ADMIN_PASSWORD not in captured.out + captured.err


def test_cli_second_run_applies_nothing_and_exits_zero(
    fresh_provisioned: Provisioned, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main([], cli_environ(fresh_provisioned)) == 0
    capsys.readouterr()
    rows_before = _tracking_rows(fresh_provisioned)

    exit_code = main([], cli_environ(fresh_provisioned))

    captured = capsys.readouterr()
    assert exit_code == 0, captured.err
    assert "applied 0 ordinary + 0 graph_gateway migration(s)" in captured.out
    assert "none (already up to date)" in captured.out
    assert _tracking_rows(fresh_provisioned) == rows_before


def test_provision_returns_component_qualified_ordinary_names_and_bare_graph_gateway_names(
    fresh_provisioned: Provisioned,
) -> None:
    result = provision(fresh_provisioned.provisioning_target())

    assert result == ProvisionResult(
        ordinary_applied=[f"{component}/{name}" for component, name in _ORDINARY_FILES],
        graph_gateway_applied=["0001_graph_mutation_log.sql"],
    )


def test_cli_on_already_migrated_database_keeps_rows_and_applies_only_missing(
    fresh_provisioned: Provisioned, capsys: pytest.CaptureFixture[str]
) -> None:
    migrate_state_database(fresh_provisioned)
    index_names = (
        "audit_events_details_celex_idx",
        "audit_events_details_regulatory_instrument_id_idx",
        "audit_events_details_instrument_id_idx",
    )
    with fresh_provisioned.as_state() as conn:
        conn.execute(
            "INSERT INTO audit_events (actor_subject, actor_issuer, action, resource_type, "
            "resource_id, outcome) VALUES ('legacy', 'i', 'x', 'thing', 'r1', 'applied')"
        )
        row_before = conn.execute(
            "SELECT actor_subject, actor_issuer, action, resource_type, resource_id, outcome "
            "FROM audit_events"
        ).fetchall()
        conn.execute(
            "DELETE FROM ps_schema_migrations WHERE component = 'audit' "
            "AND filename = '0002_audit_events_details_indexes.sql'"
        )
        for index_name in index_names:
            conn.execute(sql.SQL("DROP INDEX {}").format(sql.Identifier(index_name)))

    result = provision(fresh_provisioned.provisioning_target())

    assert result.ordinary_applied == ["audit/0002_audit_events_details_indexes.sql"]
    assert result.graph_gateway_applied == ["0001_graph_mutation_log.sql"]
    with fresh_provisioned.as_state() as conn:
        assert (
            conn.execute(
                "SELECT actor_subject, actor_issuer, action, resource_type, resource_id, outcome "
                "FROM audit_events"
            ).fetchall()
            == row_before
        )
        present = {
            str(row[0])
            for row in conn.execute(
                "SELECT indexname FROM pg_indexes WHERE tablename = 'audit_events'"
            ).fetchall()
        }
        assert set(index_names) <= present
        assert (
            scalar(
                conn,
                "SELECT tableowner FROM pg_tables WHERE tablename = 'audit_events'",
            )
            == fresh_provisioned.state_user
        )
    capsys.readouterr()


def _ownership_snapshot(prov: Provisioned) -> list[tuple[str, str, str, str, str]]:
    """Return (kind, name, owner, acl, definition) of every `public` object, role-name-free."""
    with prov.superuser_connect(prov.state_db) as conn:
        classes = conn.execute(
            "SELECT 'relation', c.relname || ':' || c.relkind::text, pg_get_userbyid(c.relowner), "
            "coalesce(c.relacl::text, ''), '' FROM pg_class c "
            "WHERE c.relnamespace = 'public'::regnamespace ORDER BY 2"
        ).fetchall()
        functions = conn.execute(
            "SELECT 'function', p.proname, pg_get_userbyid(p.proowner), "
            "coalesce(p.proacl::text, ''), pg_get_functiondef(p.oid) FROM pg_proc p "
            "WHERE p.pronamespace = 'public'::regnamespace ORDER BY 2"
        ).fetchall()
        schema = conn.execute(
            "SELECT 'schema', nspname, pg_get_userbyid(nspowner), coalesce(nspacl::text, ''), '' "
            "FROM pg_namespace WHERE nspname = 'public'"
        ).fetchall()
    return [
        tuple(str(value).replace(prov.state_user, "APP_ROLE") for value in row)  # pyright: ignore[reportReturnType]  # five-column rows
        for row in (*classes, *functions, *schema)
    ]


def test_public_objects_are_owned_by_ps_state_identically_to_service_startup(
    fresh_provisioned: Provisioned,
) -> None:
    startup_prov, startup_names = create_provisioned()
    try:
        migrate_state_database(startup_prov)  # the pre-follow-up path: service startup as ps_state
        assert main([], cli_environ(fresh_provisioned)) == 0

        via_cli = _ownership_snapshot(fresh_provisioned)
        via_startup = _ownership_snapshot(startup_prov)
    finally:
        drop_cluster_objects(startup_names)

    assert via_cli == via_startup
    assert via_cli
    assert all(owner == "APP_ROLE" for kind, _, owner, _, _ in via_cli if kind != "schema")


def test_admin_that_cannot_set_role_to_the_app_role_fails_with_a_sanitized_message(
    fresh_provisioned: Provisioned, capsys: pytest.CaptureFixture[str]
) -> None:
    admin = f"nomember_admin_{uuid.uuid4().hex[:8]}"
    with fresh_provisioned.superuser_connect() as conn:
        conn.execute(sql.SQL("CREATE ROLE {} LOGIN NOSUPERUSER").format(sql.Identifier(admin)))
        conn.execute(
            sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                sql.Identifier(fresh_provisioned.state_db), sql.Identifier(admin)
            )
        )
    try:
        exit_code = main([], cli_environ(fresh_provisioned, admin_user=admin))
    finally:
        with fresh_provisioned.superuser_connect() as conn:
            conn.execute(
                sql.SQL("REVOKE CONNECT ON DATABASE {} FROM {}").format(
                    sql.Identifier(fresh_provisioned.state_db), sql.Identifier(admin)
                )
            )
            conn.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(admin)))

    captured = capsys.readouterr()
    assert exit_code == 1
    assert f"cannot SET ROLE {fresh_provisioned.state_user}" in captured.err
    assert ADMIN_PASSWORD not in captured.out + captured.err
    assert fresh_provisioned.host not in captured.out + captured.err
    with fresh_provisioned.superuser_connect(fresh_provisioned.state_db) as conn:
        assert scalar(conn, "SELECT to_regnamespace('graph_log') IS NULL") is True


@pytest.fixture(name="racing_cluster")
def _racing_cluster() -> Iterator[Provisioned]:  # pyright: ignore[reportUnusedFunction]  # pytest fixture used by name
    prov, names = create_provisioned()
    yield prov
    drop_cluster_objects(names)


def test_service_style_and_cli_style_runs_on_one_empty_database_both_succeed(
    racing_cluster: Provisioned,
) -> None:
    failures: list[BaseException] = []
    start = threading.Barrier(2)

    def service_style() -> None:
        try:
            start.wait(timeout=30)
            with connect_from_config(racing_cluster.state_config()) as conn:
                apply_pending_migrations(conn, sources=ORDINARY_STATE_MIGRATION_SOURCES)
        except BaseException as exc:  # noqa: BLE001  # surfaced to the test thread below
            failures.append(exc)

    def cli_style() -> None:
        try:
            start.wait(timeout=30)
            provision(racing_cluster.provisioning_target())
        except BaseException as exc:  # noqa: BLE001  # surfaced to the test thread below
            failures.append(exc)

    threads = [threading.Thread(target=service_style), threading.Thread(target=cli_style)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=120)

    assert failures == []
    assert _tracking_rows(racing_cluster) == sorted(
        [*_ORDINARY_FILES, ("graph_gateway", "0001_graph_mutation_log.sql")]
    )
