"""`postgres_live` tests for the privileged provisioning path of the graph mutation log (#205).

Provisions a scratch PS Postgres with the REAL Helm init script, applies the ordinary state
migrations as `ps_state`, then runs the privileged provisioning path (admin credential) and
proves with real connections that the immutable tables exist, are owned by a non-login owner
role, and that `ps_state` -- which holds only INSERT and SELECT (plus UPDATE on the applied
marker) -- cannot rewrite, alter, drop or bypass them (AC-BI-001, AC-BI-005, AC-BI-011).

Deselected by default -- run with `uv run pytest -m postgres_live`; needs
`PS_TEST_POSTGRES_SUPERUSER_DSN` and `psql` on `PATH` (see `provisioned_postgres.py`).
"""

from __future__ import annotations

import subprocess
import sys
import uuid
from typing import TYPE_CHECKING

import psycopg
import pytest
from persistence.provisioned_postgres import (
    Provisioned,
    migrate_state_database,
    provision_graph_log,
)
from psycopg import sql

from ps_service.graph_gateway.provision import ProvisionResult
from ps_service.persistence import StatePostgresProvisioningError

if TYPE_CHECKING:
    from typing import LiteralString

pytestmark = pytest.mark.postgres_live

_TABLES = ("groups", "entries", "payloads", "checkpoints", "applied_markers")
_IMMUTABLE_TABLES = ("groups", "entries", "payloads", "checkpoints")
_MIGRATION_ROW = ("graph_gateway", "0001_graph_mutation_log.sql")
_HASH_ABC = "sha256:" + "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
_HASH_EMPTY = "sha256:" + "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


def _graph() -> str:
    return f"graph-{uuid.uuid4().hex[:8]}"


def _append_group(
    conn: psycopg.Connection[tuple[object, ...]], graph: str, positions: range
) -> None:
    """Insert one group plus its entries as the connection's role, in one transaction."""
    with conn.transaction():
        group = conn.execute(
            "INSERT INTO graph_log.groups (graph, first_position, last_position) "
            "VALUES (%s, %s, %s) RETURNING group_id",
            (graph, positions.start, positions[-1]),
        ).fetchone()
        assert group is not None
        for position in positions:
            conn.execute(
                "INSERT INTO graph_log.entries "
                "(graph, position, group_id, name, identity, content) "
                "VALUES (%s, %s, %s, 'Capability', %s, '{}'::jsonb)",
                (graph, position, group[0], f"id-{position}"),
            )


def _scalar(conn: psycopg.Connection[tuple[object, ...]], query: str, *params: object) -> object:
    row = conn.execute(query, params).fetchone()  # pyright: ignore[reportArgumentType]  # test-only dynamic SQL
    assert row is not None
    return row[0]


def _privilege_snapshot(prov: Provisioned) -> dict[str, object]:
    """Role-name-independent ownership and privilege facts about the graph_log objects."""
    with prov.superuser_connect(prov.state_db) as conn:
        owners = conn.execute(
            "SELECT tablename, tableowner = %s FROM pg_tables WHERE schemaname = 'graph_log' "
            "ORDER BY tablename",
            (prov.owner_role,),
        ).fetchall()
        privileges = conn.execute(
            "SELECT table_name, privilege_type FROM information_schema.role_table_grants "
            "WHERE table_schema = 'graph_log' AND grantee = %s ORDER BY 1, 2",
            (prov.state_user,),
        ).fetchall()
        schema_owner = _scalar(
            conn,
            "SELECT nspowner::regrole::text = %s FROM pg_namespace WHERE nspname = 'graph_log'",
            prov.owner_role,
        )
    return {"owners": owners, "privileges": privileges, "schema_owner": schema_owner}


def test_provisioning_on_empty_ps_state_creates_five_tables_and_records_migration(
    fresh_provisioned: Provisioned,
) -> None:
    migrate_state_database(fresh_provisioned)

    applied = provision_graph_log(fresh_provisioned)

    assert applied.graph_gateway_applied == [_MIGRATION_ROW[1]]
    with fresh_provisioned.as_state() as conn:
        tables = conn.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'graph_log' ORDER BY 1"
        ).fetchall()
        recorded = conn.execute(
            "SELECT component, filename FROM ps_schema_migrations WHERE component = 'graph_gateway'"
        ).fetchall()
    assert [row[0] for row in tables] == sorted(_TABLES)
    assert recorded == [_MIGRATION_ROW]


def test_second_provisioning_run_applies_nothing(fresh_provisioned: Provisioned) -> None:
    migrate_state_database(fresh_provisioned)
    provision_graph_log(fresh_provisioned)

    assert provision_graph_log(fresh_provisioned) == ProvisionResult([], [])


def test_provisioning_keeps_the_migration_tracking_table_owned_by_the_state_role(
    fresh_provisioned: Provisioned,
) -> None:
    migrate_state_database(fresh_provisioned)

    provision_graph_log(fresh_provisioned)

    with fresh_provisioned.superuser_connect(fresh_provisioned.state_db) as conn:
        owner = _scalar(
            conn, "SELECT tableowner FROM pg_tables WHERE tablename = 'ps_schema_migrations'"
        )
    assert owner == fresh_provisioned.state_user


def test_provisioned_tables_are_owned_by_non_login_owner_role(
    provisioned_graph_log: Provisioned,
) -> None:
    snapshot = _privilege_snapshot(provisioned_graph_log)

    assert snapshot["owners"] == [(table, True) for table in sorted(_TABLES)]
    assert snapshot["schema_owner"] is True
    with provisioned_graph_log.superuser_connect() as conn:
        can_login = _scalar(
            conn,
            "SELECT rolcanlogin FROM pg_roles WHERE rolname = %s",
            provisioned_graph_log.owner_role,
        )
        is_member = _scalar(
            conn,
            "SELECT pg_has_role(%s, %s, 'MEMBER')",
            provisioned_graph_log.state_user,
            provisioned_graph_log.owner_role,
        )
    assert can_login is False
    assert is_member is False


def test_state_role_holds_exactly_insert_and_select_and_update_only_on_applied_markers(
    provisioned_graph_log: Provisioned,
) -> None:
    privileges = _privilege_snapshot(provisioned_graph_log)["privileges"]

    assert privileges == sorted(
        [
            *[
                (table, privilege)
                for table in _IMMUTABLE_TABLES
                for privilege in ("INSERT", "SELECT")
            ],
            ("applied_markers", "INSERT"),
            ("applied_markers", "SELECT"),
            ("applied_markers", "UPDATE"),
        ]
    )


_DENIED_MUTATIONS: list[LiteralString] = [
    "UPDATE graph_log.groups SET graph = graph",
    "UPDATE graph_log.entries SET graph = graph",
    "UPDATE graph_log.payloads SET kind = kind",
    "UPDATE graph_log.checkpoints SET graph = graph",
    "DELETE FROM graph_log.groups",
    "DELETE FROM graph_log.entries",
    "DELETE FROM graph_log.payloads",
    "DELETE FROM graph_log.checkpoints",
    "TRUNCATE graph_log.groups",
    "TRUNCATE graph_log.entries",
    "TRUNCATE graph_log.payloads",
    "TRUNCATE graph_log.checkpoints",
]


@pytest.mark.parametrize("statement", _DENIED_MUTATIONS)
def test_state_role_update_delete_truncate_on_immutable_tables_are_denied(
    provisioned_graph_log: Provisioned, statement: LiteralString
) -> None:
    with (
        provisioned_graph_log.as_state() as conn,
        pytest.raises(psycopg.errors.InsufficientPrivilege),
    ):
        conn.execute(statement)


@pytest.mark.parametrize(
    "statement",
    [
        "ALTER TABLE graph_log.entries ADD COLUMN extra int",
        "ALTER TABLE graph_log.entries RENAME TO entries_renamed",
        "DROP TABLE graph_log.entries",
        "DROP TABLE graph_log.applied_markers",
        "ALTER TABLE graph_log.entries DISABLE TRIGGER ALL",
        "ALTER TABLE graph_log.entries DISABLE TRIGGER entries_next_position",
        "ALTER TABLE graph_log.entries DROP CONSTRAINT entries_position_positive",
        "ALTER TABLE graph_log.entries OWNER TO CURRENT_USER",
        "DROP TRIGGER entries_next_position ON graph_log.entries",
        "DROP FUNCTION graph_log.entries_next_position()",
        (
            "CREATE OR REPLACE FUNCTION graph_log.entries_next_position() RETURNS trigger "
            "LANGUAGE plpgsql AS 'BEGIN RETURN NEW; END'"
        ),
        "CREATE TABLE graph_log.sneaky (id int)",
    ],
    ids=lambda statement: statement[:60],
)
def test_state_role_cannot_alter_drop_or_disable_triggers_or_constraints_on_immutable_tables(
    provisioned_graph_log: Provisioned, statement: LiteralString
) -> None:
    with (
        provisioned_graph_log.as_state() as conn,
        pytest.raises(psycopg.errors.InsufficientPrivilege),
    ):
        conn.execute(statement)  # pyright: ignore[reportArgumentType]


def test_state_role_cannot_set_role_to_owner_or_grant_membership(
    provisioned_graph_log: Provisioned,
) -> None:
    owner = sql.Identifier(provisioned_graph_log.owner_role)
    with provisioned_graph_log.as_state() as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute(sql.SQL("SET ROLE {}").format(owner))
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute(
                sql.SQL("GRANT {} TO {}").format(
                    owner, sql.Identifier(provisioned_graph_log.state_user)
                )
            )


def test_state_role_cannot_drop_graph_log_schema(provisioned_graph_log: Provisioned) -> None:
    with (
        provisioned_graph_log.as_state() as conn,
        pytest.raises(psycopg.errors.InsufficientPrivilege),
    ):
        conn.execute("DROP SCHEMA graph_log CASCADE")


def test_state_role_can_insert_and_select_on_immutable_tables_and_update_only_applied_markers(
    provisioned_graph_log: Provisioned,
) -> None:
    graph = _graph()
    with provisioned_graph_log.as_state() as conn:
        audit_event_id = _scalar(
            conn,
            "INSERT INTO audit_events (actor_subject, actor_issuer, action, resource_type, "
            "resource_id, outcome) VALUES ('a', 'i', 'x', 'graph', %s, 'applied') RETURNING id",
            graph,
        )
        conn.execute(
            "INSERT INTO graph_log.payloads (payload_hash, kind, byte_length, body) "
            "VALUES (%s, 'float64le', 3, %s), (%s, 'json', 0, %s)",
            (_HASH_ABC, b"abc", _HASH_EMPTY, b""),
        )
        with conn.transaction():
            group_id = _scalar(
                conn,
                "INSERT INTO graph_log.groups (graph, first_position, last_position, "
                "audit_event_id) VALUES (%s, 1, 2, %s) RETURNING group_id",
                graph,
                audit_event_id,
            )
            conn.execute(
                "INSERT INTO graph_log.entries (graph, position, group_id, name, identity, "
                "content, embedding_payload_hash) "
                "VALUES (%s, 1, %s, 'Capability', 'a', '{}'::jsonb, %s)",
                (graph, group_id, _HASH_ABC),
            )
            conn.execute(
                "INSERT INTO graph_log.entries (graph, position, group_id, name, identity, "
                "content_payload_hash) VALUES (%s, 2, %s, 'Capability', 'b', %s)",
                (graph, group_id, _HASH_EMPTY),
            )
        conn.execute(
            "INSERT INTO graph_log.checkpoints (graph, position, canonical_digest) "
            "VALUES (%s, 2, 'digest')",
            (graph,),
        )
        conn.execute(
            "INSERT INTO graph_log.applied_markers (graph, applied_position) VALUES (%s, 1)",
            (graph,),
        )
        conn.execute(
            "UPDATE graph_log.applied_markers SET applied_position = 2 WHERE graph = %s", (graph,)
        )

        counts = {
            table: _scalar(
                conn,
                sql.SQL("SELECT count(*) FROM graph_log.{}").format(  # pyright: ignore[reportArgumentType]
                    sql.Identifier(table)
                ),
            )
            for table in _TABLES
        }
        marker = _scalar(
            conn, "SELECT applied_position FROM graph_log.applied_markers WHERE graph = %s", graph
        )

    assert all(isinstance(count, int) and count >= 1 for count in counts.values())
    assert marker == 2


def test_state_role_cannot_insert_a_position_gap(provisioned_graph_log: Provisioned) -> None:
    graph = _graph()
    _append_group(provisioned_graph_log.as_state(), graph, range(1, 3))

    with pytest.raises(psycopg.errors.CheckViolation):
        _append_group(provisioned_graph_log.as_state(), graph, range(5, 6))


def test_state_role_cannot_start_a_graph_above_position_one(
    provisioned_graph_log: Provisioned,
) -> None:
    with pytest.raises(psycopg.errors.CheckViolation):
        _append_group(provisioned_graph_log.as_state(), _graph(), range(2, 4))


def _insert_group(
    conn: psycopg.Connection[tuple[object, ...]], graph: str, first: int, last: int
) -> object:
    return _scalar(
        conn,
        "INSERT INTO graph_log.groups (graph, first_position, last_position) "
        "VALUES (%s, %s, %s) RETURNING group_id",
        graph,
        first,
        last,
    )


def _insert_entry(  # one parameter per column under test
    conn: psycopg.Connection[tuple[object, ...]],
    graph: str,
    position: int,
    group_id: object,
    content: str | None = "{}",
    content_payload_hash: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO graph_log.entries (graph, position, group_id, name, identity, content, "
        "content_payload_hash) VALUES (%s, %s, %s, 'Capability', 'x', %s::jsonb, %s)",
        (graph, position, group_id, content, content_payload_hash),
    )


def _write_group_claiming_three_entries_but_holding_two(
    conn: psycopg.Connection[tuple[object, ...]], graph: str
) -> None:
    with conn.transaction():
        group_id = _insert_group(conn, graph, 1, 3)
        for position in (1, 2):
            _insert_entry(conn, graph, position, group_id)


def test_state_role_cannot_insert_group_whose_range_disagrees_with_its_entries(
    provisioned_graph_log: Provisioned,
) -> None:
    graph = _graph()
    with provisioned_graph_log.as_state() as conn:
        with pytest.raises(psycopg.errors.CheckViolation):
            _write_group_claiming_three_entries_but_holding_two(conn, graph)
        assert _scalar(conn, "SELECT count(*) FROM graph_log.entries WHERE graph = %s", graph) == 0


def test_state_role_cannot_insert_a_group_with_no_entries(
    provisioned_graph_log: Provisioned,
) -> None:
    with (
        provisioned_graph_log.as_state() as conn,
        pytest.raises(psycopg.errors.CheckViolation),
        conn.transaction(),
    ):
        conn.execute(
            "INSERT INTO graph_log.groups (graph, first_position, last_position) VALUES (%s, 1, 1)",
            (_graph(),),
        )


def _write_entry_under_a_group_of_another_graph(
    conn: psycopg.Connection[tuple[object, ...]],
) -> None:
    with conn.transaction():
        group_id = _insert_group(conn, "graph-a", 1, 1)
        _insert_entry(conn, _graph(), 1, group_id)


def test_state_role_cannot_insert_an_entry_whose_graph_differs_from_its_group(
    provisioned_graph_log: Provisioned,
) -> None:
    with (
        provisioned_graph_log.as_state() as conn,
        pytest.raises(psycopg.errors.ForeignKeyViolation),
    ):
        _write_entry_under_a_group_of_another_graph(conn)


def test_state_role_cannot_update_applied_marker_past_last_entry(
    provisioned_graph_log: Provisioned,
) -> None:
    graph = _graph()
    _append_group(provisioned_graph_log.as_state(), graph, range(1, 3))
    with provisioned_graph_log.as_state() as conn:
        conn.execute(
            "INSERT INTO graph_log.applied_markers (graph, applied_position) VALUES (%s, 1)",
            (graph,),
        )
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                "UPDATE graph_log.applied_markers SET applied_position = 3 WHERE graph = %s",
                (graph,),
            )


def test_state_role_cannot_move_applied_marker_backwards(
    provisioned_graph_log: Provisioned,
) -> None:
    graph = _graph()
    _append_group(provisioned_graph_log.as_state(), graph, range(1, 3))
    with provisioned_graph_log.as_state() as conn:
        conn.execute(
            "INSERT INTO graph_log.applied_markers (graph, applied_position) VALUES (%s, 2)",
            (graph,),
        )
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                "UPDATE graph_log.applied_markers SET applied_position = 1 WHERE graph = %s",
                (graph,),
            )


def test_state_role_cannot_insert_applied_marker_past_last_entry(
    provisioned_graph_log: Provisioned,
) -> None:
    with (
        provisioned_graph_log.as_state() as conn,
        pytest.raises(psycopg.errors.CheckViolation),
    ):
        conn.execute(
            "INSERT INTO graph_log.applied_markers (graph, applied_position) VALUES (%s, 1)",
            (_graph(),),
        )


@pytest.mark.parametrize(
    ("statement", "params"),
    [
        pytest.param(
            "INSERT INTO graph_log.payloads (payload_hash, kind, byte_length, body) "
            "VALUES ('sha256:00', 'json', 3, 'abc')",
            (),
            id="payload-hash-does-not-match-body",
        ),
        pytest.param(
            "INSERT INTO graph_log.payloads (payload_hash, kind, byte_length, body) "
            "VALUES (%s, 'json', 4, 'abc')",
            (_HASH_ABC,),
            id="payload-byte-length-does-not-match-body",
        ),
        pytest.param(
            "INSERT INTO graph_log.payloads (payload_hash, kind, byte_length, body) "
            "VALUES (%s, 'jsonb', 3, 'abc')",
            (_HASH_ABC,),
            id="payload-kind-unknown",
        ),
        pytest.param(
            "INSERT INTO graph_log.checkpoints (graph, position, canonical_digest) "
            "VALUES ('g', 1, '')",
            (),
            id="checkpoint-empty-digest",
        ),
        pytest.param(
            "INSERT INTO graph_log.groups (graph, first_position, last_position) "
            "VALUES ('g', 0, 1)",
            (),
            id="group-first-position-below-one",
        ),
        pytest.param(
            "INSERT INTO graph_log.groups (graph, first_position, last_position) "
            "VALUES ('g', 3, 2)",
            (),
            id="group-last-position-before-first",
        ),
        pytest.param(
            "INSERT INTO graph_log.applied_markers (graph, applied_position) VALUES ('g', -1)",
            (),
            id="marker-negative",
        ),
    ],
)
def test_state_role_cannot_insert_a_row_violating_a_table_check(
    provisioned_graph_log: Provisioned, statement: str, params: tuple[str, ...]
) -> None:
    with (
        provisioned_graph_log.as_state() as conn,
        pytest.raises(psycopg.errors.CheckViolation),
        conn.transaction(),
    ):
        conn.execute(statement, params)  # pyright: ignore[reportArgumentType]


def _write_entry_with_content_sources(
    conn: psycopg.Connection[tuple[object, ...]], content: str | None, payload_hash: str | None
) -> None:
    with conn.transaction():
        group_id = _insert_group(conn, _graph(), 1, 1)
        graph = _scalar(conn, "SELECT graph FROM graph_log.groups WHERE group_id = %s", group_id)
        assert isinstance(graph, str)
        _insert_entry(conn, graph, 1, group_id, content, payload_hash)


@pytest.mark.parametrize(
    ("content", "payload_hash"),
    [
        pytest.param(None, None, id="entry-with-neither-inline-content-nor-payload"),
        pytest.param("{}", _HASH_ABC, id="entry-with-both-inline-content-and-payload"),
    ],
)
def test_state_role_cannot_insert_an_entry_without_exactly_one_content_source(
    provisioned_graph_log: Provisioned, content: str | None, payload_hash: str | None
) -> None:
    with provisioned_graph_log.as_state() as conn:
        conn.execute(
            "INSERT INTO graph_log.payloads (payload_hash, kind, byte_length, body) "
            "VALUES (%s, 'json', 3, %s) ON CONFLICT DO NOTHING",
            (_HASH_ABC, b"abc"),
        )
        with pytest.raises(psycopg.errors.CheckViolation):
            _write_entry_with_content_sources(conn, content, payload_hash)


def test_provisioning_refuses_when_app_role_is_member_of_owner_role(
    fresh_provisioned: Provisioned,
) -> None:
    migrate_state_database(fresh_provisioned)
    with fresh_provisioned.superuser_connect() as conn:
        conn.execute(
            sql.SQL("GRANT {} TO {}").format(
                sql.Identifier(fresh_provisioned.owner_role),
                sql.Identifier(fresh_provisioned.state_user),
            )
        )

    with pytest.raises(StatePostgresProvisioningError, match="member"):
        provision_graph_log(fresh_provisioned)

    with fresh_provisioned.superuser_connect(fresh_provisioned.state_db) as conn:
        assert _scalar(conn, "SELECT to_regnamespace('graph_log') IS NULL") is True


def test_provisioning_refuses_when_the_owner_role_can_log_in(
    fresh_provisioned: Provisioned,
) -> None:
    migrate_state_database(fresh_provisioned)
    with fresh_provisioned.superuser_connect() as conn:
        conn.execute(
            sql.SQL("ALTER ROLE {} LOGIN").format(sql.Identifier(fresh_provisioned.owner_role))
        )

    with pytest.raises(StatePostgresProvisioningError, match="NOLOGIN"):
        provision_graph_log(fresh_provisioned)


def test_upgrade_existing_deployment_keeps_ps_state_rows_and_gains_same_ownership_and_privileges(
    fresh_provisioned: Provisioned, provisioned_graph_log: Provisioned
) -> None:
    migrate_state_database(fresh_provisioned)
    with fresh_provisioned.superuser_connect() as admin:
        # The pre-#205 init script never created the owner role; emulate that cluster.
        admin.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(fresh_provisioned.owner_role)))
    with fresh_provisioned.as_state() as conn:
        conn.execute(
            "INSERT INTO audit_events (actor_subject, actor_issuer, action, resource_type, "
            "resource_id, outcome) VALUES ('legacy', 'i', 'x', 'thing', 'r1', 'applied')"
        )
        before = _scalar(conn, "SELECT count(*) FROM audit_events")

    provision_graph_log(fresh_provisioned)

    with fresh_provisioned.as_state() as conn:
        assert _scalar(conn, "SELECT count(*) FROM audit_events") == before
        assert (
            _scalar(conn, "SELECT count(*) FROM audit_events WHERE actor_subject = 'legacy'") == 1
        )
    assert _privilege_snapshot(fresh_provisioned) == _privilege_snapshot(provisioned_graph_log)


def _run_cli(prov: Provisioned, password: str) -> subprocess.CompletedProcess[str]:
    import os  # only this helper needs the process environment

    env = {
        **os.environ,
        "PS_STATE_POSTGRES_HOST": prov.host,
        "PS_STATE_POSTGRES_PORT": str(prov.port),
        "PS_STATE_POSTGRES_DATABASE": prov.state_db,
        "PS_STATE_POSTGRES_USER": prov.state_user,
        "PS_STATE_ADMIN_POSTGRES_USER": prov.superuser,
        "PS_STATE_ADMIN_POSTGRES_PASSWORD": password,
        "PS_STATE_GRAPH_OWNER_ROLE": prov.owner_role,
    }
    return subprocess.run(  # fixed argv, this interpreter, no shell
        [sys.executable, "-m", "ps_service.graph_gateway.provision"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )


def test_cli_main_provisions_from_environment_and_logs_no_password(
    fresh_provisioned: Provisioned,
) -> None:
    migrate_state_database(fresh_provisioned)
    password = f"admin-pw-{uuid.uuid4().hex}"

    completed = _run_cli(fresh_provisioned, password)

    assert completed.returncode == 0, completed.stderr
    assert password not in completed.stdout
    assert password not in completed.stderr
    assert "0001_graph_mutation_log.sql" in completed.stdout
    with fresh_provisioned.as_state() as conn:
        assert _scalar(conn, "SELECT count(*) FROM graph_log.groups") == 0
