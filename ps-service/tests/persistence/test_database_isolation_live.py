"""`postgres_live` tests for the PS Postgres init script's database isolation (issue #130 S5).

Runs the SAME `charts/policy-system/files/ps-postgres-init.sh` the Helm chart mounts into
the Postgres image (via `templates/ps-postgres-init-configmap.yaml`) against a scratch
server, with unique database/role names per session, then proves AC-BI-016 with real
connections: neither role can connect to the other's database, a mistakenly re-granted
`CONNECT` still leaves the other database's tables unreadable and `public` unwritable, and
`PUBLIC` holds neither `CONNECT` nor `CREATE` on `public` in either database. It also proves
the least-privilege claim: migrations and the catalog-source tool body work as the `ps_state`
role alone.

Deselected by default -- run with `uv run pytest -m postgres_live`. Needs
`PS_TEST_POSTGRES_SUPERUSER_DSN` (a superuser DSN of a scratch server, e.g.
`postgresql://postgres@127.0.0.1:54329/postgres`) and `psql` on `PATH`.
"""

from __future__ import annotations

import dataclasses
import uuid

import psycopg
import pytest
from psycopg import sql

from persistence.provisioned_postgres import (
    STATE_SOURCES,
    Provisioned,
    drop_cluster_objects,
    init_names,
    migrate_state_database,
    provision_graph_log,
    run_init_script,
    superuser_params,
)
from ps_service.audit import PsycopgAuditStore
from ps_service.config import ServiceConfig, load_config
from ps_service.curated_source.store import get_override, reset_override, set_override
from ps_service.persistence import apply_pending_migrations, connect_from_config
from ps_service.runtime_config import PsycopgRuntimeConfigStore

pytestmark = pytest.mark.postgres_live

_ISSUER = "https://issuer.example.com/"
_GRAPH_LOG_TABLES = ("groups", "entries", "payloads", "checkpoints", "applied_markers")
_TABLE_PRIVILEGES = ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER")


def test_init_script_output_never_contains_a_password() -> None:
    names = init_names(uuid.uuid4().hex[:8])

    result = run_init_script(superuser_params(), names)

    try:
        assert result.returncode == 0, result.stderr
        for secret in (
            names["PS_STATE_POSTGRES_PASSWORD"],
            names["PS_PASSKEYSIGNING_POSTGRES_PASSWORD"],
        ):
            assert secret not in result.stdout
            assert secret not in result.stderr
    finally:
        drop_cluster_objects(names)


def test_each_role_can_connect_and_create_in_its_own_database(provisioned: Provisioned) -> None:
    with provisioned.as_state() as conn:
        conn.execute("CREATE TABLE own_state_probe (id int)")
    with provisioned.as_signing() as conn:
        conn.execute("CREATE TABLE own_signing_probe (id int)")


def test_signing_role_cannot_connect_to_state_database(provisioned: Provisioned) -> None:
    with pytest.raises(psycopg.OperationalError, match="permission denied for database"):
        provisioned.as_signing(provisioned.state_db).close()


def test_state_role_cannot_connect_to_signing_database(provisioned: Provisioned) -> None:
    with pytest.raises(psycopg.OperationalError, match="permission denied for database"):
        provisioned.as_state(provisioned.signing_db).close()


def test_role_cannot_read_or_create_in_other_database_even_when_connect_is_regranted(
    provisioned: Provisioned,
) -> None:
    with provisioned.as_state() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS victim_state_table (id int)")
        conn.execute("INSERT INTO victim_state_table VALUES (1)")
    with provisioned.superuser_connect() as admin:
        admin.execute(
            sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                sql.Identifier(provisioned.state_db), sql.Identifier(provisioned.signing_user)
            )
        )
    try:
        with provisioned.as_signing(provisioned.state_db) as conn:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute("SELECT * FROM victim_state_table")
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute("CREATE TABLE intruder_table (id int)")
    finally:
        with provisioned.superuser_connect() as admin:
            admin.execute(
                sql.SQL("REVOKE CONNECT ON DATABASE {} FROM {}").format(
                    sql.Identifier(provisioned.state_db), sql.Identifier(provisioned.signing_user)
                )
            )


def test_public_has_no_connect_and_no_create_on_public_schema_in_either_database(
    provisioned: Provisioned,
) -> None:
    with provisioned.superuser_connect() as conn:
        for database in (provisioned.state_db, provisioned.signing_db):
            row = conn.execute(
                "SELECT has_database_privilege('public', %s, 'CONNECT')", (database,)
            ).fetchone()
            assert row == (False,), f"PUBLIC still has CONNECT on {database}"
    for database in (provisioned.state_db, provisioned.signing_db):
        with provisioned.superuser_connect(database) as conn:
            row = conn.execute(
                "SELECT has_schema_privilege('public', 'public', 'CREATE')"
            ).fetchone()
            assert row == (False,), f"PUBLIC still has CREATE on schema public in {database}"


def test_init_script_rerun_fails_loudly_and_never_silently_succeeds(
    provisioned: Provisioned,
) -> None:
    names = {
        "PS_STATE_DATABASE": provisioned.state_db,
        "PS_STATE_USER": provisioned.state_user,
        "PS_SIGNING_DATABASE": provisioned.signing_db,
        "PS_SIGNING_USER": provisioned.signing_user,
        "PS_STATE_POSTGRES_PASSWORD": provisioned.state_password,
        "PS_PASSKEYSIGNING_POSTGRES_PASSWORD": provisioned.signing_password,
    }

    result = run_init_script(superuser_params(), names)

    assert result.returncode != 0
    assert "already exists" in result.stderr


def test_migrations_and_catalog_tool_work_as_the_least_privilege_state_role(
    provisioned: Provisioned,
) -> None:
    config: ServiceConfig = dataclasses.replace(
        load_config(),
        state_postgres_host=provisioned.host,
        state_postgres_port=provisioned.port,
        state_postgres_database=provisioned.state_db,
        state_postgres_user=provisioned.state_user,
        state_postgres_password=provisioned.state_password,
    )
    actor = ("least-privilege-actor", _ISSUER)
    store = PsycopgRuntimeConfigStore(config, audit_store=PsycopgAuditStore(config))

    with connect_from_config(config) as conn:
        apply_pending_migrations(conn, sources=STATE_SOURCES)
    set_override(store, "https://example.com/least-privilege", actor=actor)

    assert get_override(store) == "https://example.com/least-privilege"
    reset_override(store, actor=actor)
    assert get_override(store) is None
    with provisioned.as_state() as conn:
        row = conn.execute(
            "SELECT count(*) FROM audit_events WHERE actor_subject = %s", ("least-privilege-actor",)
        ).fetchone()
    assert row is not None
    assert row[0] == 2


def test_init_script_creates_nologin_owner_role_and_state_role_is_not_a_member(
    provisioned: Provisioned,
) -> None:
    with provisioned.superuser_connect() as conn:
        role = conn.execute(
            "SELECT rolcanlogin, rolsuper FROM pg_roles WHERE rolname = %s",
            (provisioned.owner_role,),
        ).fetchone()
        is_member = conn.execute(
            "SELECT pg_has_role(%s, %s, 'MEMBER')", (provisioned.state_user, provisioned.owner_role)
        ).fetchone()

    assert role == (False, False)
    assert is_member == (False,)


def test_owner_role_cannot_log_in(provisioned: Provisioned) -> None:
    with pytest.raises(psycopg.OperationalError, match="not permitted to log in"):
        provisioned.connect_as(provisioned.owner_role, "irrelevant", provisioned.state_db).close()


def test_init_then_provisioning_yields_owner_owned_tables_with_insert_select_only_for_state_role(
    fresh_provisioned: Provisioned,
) -> None:
    migrate_state_database(fresh_provisioned)

    provision_graph_log(fresh_provisioned)

    with fresh_provisioned.superuser_connect(fresh_provisioned.state_db) as conn:
        owners = conn.execute(
            "SELECT DISTINCT tableowner FROM pg_tables WHERE schemaname = 'graph_log'"
        ).fetchall()
        granted = {
            (table, privilege): conn.execute(
                "SELECT has_table_privilege(%s, %s, %s)",
                (fresh_provisioned.state_user, f"graph_log.{table}", privilege),
            ).fetchone()
            for table in _GRAPH_LOG_TABLES
            for privilege in _TABLE_PRIVILEGES
        }
    assert owners == [(fresh_provisioned.owner_role,)]
    for (table, privilege), row in granted.items():
        expected = privilege in {"INSERT", "SELECT"} or (
            table == "applied_markers" and privilege == "UPDATE"
        )
        assert row == (expected,), f"{privilege} on graph_log.{table}"


def test_signing_role_still_cannot_connect_to_state_database_after_owner_role_added(
    provisioned_graph_log: Provisioned,
) -> None:
    with pytest.raises(psycopg.OperationalError, match="permission denied for database"):
        provisioned_graph_log.as_signing(provisioned_graph_log.state_db).close()
