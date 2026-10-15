"""`postgres_live` tests: nobody can change or remove an audit row once it is written.

Two layers, each proven on its own. The application role (`ps_state`) holds no UPDATE, DELETE,
TRUNCATE or row-lock privilege, so Postgres refuses it with `InsufficientPrivilege`. The owner
role and a superuser do hold those privileges, so the insert-only triggers (`ENABLE ALWAYS`)
refuse them with `RestrictViolation`. After every refused statement the table and the chain head
are compared, byte for byte, with a snapshot taken beforehand.

Deselected by default -- run with `uv run pytest -m postgres_live` (see
`persistence/provisioned_postgres.py`).
"""

from __future__ import annotations

import psycopg
import pytest
from persistence.provisioned_postgres import (
    Provisioned,
    migrate_state_database,
    provision_graph_log,
)
from psycopg import sql

pytestmark = pytest.mark.postgres_live

_ISSUER = "https://issuer.example.com/"
_TABLE = "audit_log.audit_events"

_MERGE_UPDATE = (
    "MERGE INTO audit_log.audit_events AS t USING (SELECT 1) AS s(x) ON true "
    "WHEN MATCHED THEN UPDATE SET outcome = 'failed'"
)
_MERGE_DELETE = (
    "MERGE INTO audit_log.audit_events AS t USING (SELECT 1) AS s(x) ON true "
    "WHEN MATCHED THEN DELETE"
)
_UPDATES = (
    "UPDATE audit_log.audit_events SET outcome = 'failed'",
    "UPDATE audit_log.audit_events SET outcome = 'failed' WHERE resource_id = 'row-1'",
    "UPDATE audit_log.audit_events SET row_hash = repeat('a', 64)",
    "UPDATE audit_log.audit_events SET chain_seq = chain_seq + 100",
)
_DELETES = (
    "DELETE FROM audit_log.audit_events WHERE resource_id = 'row-1'",
    "DELETE FROM audit_log.audit_events",
)
_TRUNCATES = (
    "TRUNCATE audit_log.audit_events",
    "TRUNCATE audit_log.audit_events CASCADE",
)


@pytest.fixture
def prov(fresh_provisioned: Provisioned) -> Provisioned:
    """A fully provisioned cluster holding three chained audit rows."""
    migrate_state_database(fresh_provisioned)
    provision_graph_log(fresh_provisioned)
    with fresh_provisioned.as_state() as conn:
        for n in (1, 2, 3):
            conn.execute(
                "INSERT INTO audit_log.audit_events "
                "(actor_subject, actor_issuer, action, resource_type, resource_id, outcome) "
                "VALUES ('actor', %s, 'raw.write', 'raw', %s, 'applied')",
                (_ISSUER, f"row-{n}"),
            )
    return fresh_provisioned


def _snapshot(prov: Provisioned) -> tuple[list[tuple[object, ...]], list[tuple[object, ...]]]:
    """Every audit row (all columns) and the chain head, read as superuser."""
    with prov.superuser_connect(prov.state_db) as conn:
        rows = conn.execute("SELECT * FROM audit_log.audit_events ORDER BY chain_seq").fetchall()
        head = conn.execute("SELECT * FROM audit_log.audit_chain_head").fetchall()
    return rows, head


def _as_owner(prov: Provisioned) -> psycopg.Connection[tuple[object, ...]]:
    """A superuser session that has switched to the (non-login) owner role."""
    conn = prov.superuser_connect(prov.state_db)
    conn.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(prov.owner_role)))
    return conn


def _merge_supported(prov: Provisioned) -> bool:
    with prov.superuser_connect() as conn:
        row = conn.execute("SELECT current_setting('server_version_num')::int").fetchone()
    assert row is not None
    return int(str(row[0])) >= 150000


@pytest.mark.parametrize("statement", _UPDATES + _DELETES + _TRUNCATES)
def test_the_app_role_is_refused_by_postgres_and_nothing_changes(
    prov: Provisioned, statement: str
) -> None:
    before = _snapshot(prov)

    with prov.as_state() as conn, pytest.raises(psycopg.errors.InsufficientPrivilege):
        conn.execute(statement)  # pyright: ignore[reportArgumentType, reportCallIssue]

    assert _snapshot(prov) == before


def test_the_app_role_cannot_upsert_over_an_existing_id(prov: Provisioned) -> None:
    before = _snapshot(prov)
    with prov.superuser_connect(prov.state_db) as conn:
        existing = conn.execute("SELECT id FROM audit_log.audit_events LIMIT 1").fetchone()
    assert existing is not None

    with prov.as_state() as conn, pytest.raises(psycopg.errors.InsufficientPrivilege):
        conn.execute(
            "INSERT INTO audit_log.audit_events "
            "(id, actor_subject, actor_issuer, action, resource_type, resource_id, outcome) "
            "VALUES (%s, 'a', 'i', 'raw.write', 'raw', 'x', 'applied') "
            "ON CONFLICT (id) DO UPDATE SET outcome = 'failed'",
            (existing[0],),
        )

    assert _snapshot(prov) == before


@pytest.mark.parametrize(
    "statement",
    [
        "SELECT * FROM audit_log.audit_events FOR UPDATE",
        "SELECT * FROM audit_log.audit_events FOR SHARE",
        "SELECT * FROM audit_log.audit_events FOR NO KEY UPDATE",
    ],
)
def test_the_app_role_cannot_row_lock_the_audit_rows(prov: Provisioned, statement: str) -> None:
    with prov.as_state() as conn, pytest.raises(psycopg.errors.InsufficientPrivilege):
        conn.execute(statement)  # pyright: ignore[reportArgumentType, reportCallIssue]


@pytest.mark.parametrize("statement", [_MERGE_UPDATE, _MERGE_DELETE])
def test_the_app_role_cannot_change_rows_through_merge(prov: Provisioned, statement: str) -> None:
    if not _merge_supported(prov):
        pytest.skip("MERGE needs Postgres 15 or later")
    before = _snapshot(prov)

    with prov.as_state() as conn, pytest.raises(psycopg.errors.InsufficientPrivilege):
        conn.execute(statement)  # pyright: ignore[reportArgumentType, reportCallIssue]

    assert _snapshot(prov) == before


@pytest.mark.parametrize("statement", _UPDATES + _DELETES)
def test_the_owner_role_is_refused_by_the_insert_only_trigger(
    prov: Provisioned, statement: str
) -> None:
    before = _snapshot(prov)

    with _as_owner(prov) as conn, pytest.raises(psycopg.errors.RestrictViolation) as raised:
        conn.execute(statement)  # pyright: ignore[reportArgumentType, reportCallIssue]

    assert "insert-only" in str(raised.value)
    assert _snapshot(prov) == before


def test_the_owner_role_is_refused_truncate_by_the_statement_trigger(prov: Provisioned) -> None:
    before = _snapshot(prov)

    with _as_owner(prov) as conn, pytest.raises(psycopg.errors.RestrictViolation) as raised:
        conn.execute("TRUNCATE audit_log.audit_events CASCADE")

    assert "insert-only" in str(raised.value)
    assert _snapshot(prov) == before


@pytest.mark.parametrize("statement", [_MERGE_UPDATE, _MERGE_DELETE])
def test_the_owner_role_is_refused_merge_by_the_insert_only_trigger(
    prov: Provisioned, statement: str
) -> None:
    if not _merge_supported(prov):
        pytest.skip("MERGE needs Postgres 15 or later")
    before = _snapshot(prov)

    with _as_owner(prov) as conn, pytest.raises(psycopg.errors.RestrictViolation):
        conn.execute(statement)  # pyright: ignore[reportArgumentType, reportCallIssue]

    assert _snapshot(prov) == before


@pytest.mark.parametrize("statement", [_UPDATES[0], _DELETES[1], _TRUNCATES[1]])
def test_a_superuser_with_triggers_left_on_is_refused_even_in_replica_mode(
    prov: Provisioned, statement: str
) -> None:
    before = _snapshot(prov)

    with prov.superuser_connect(prov.state_db) as conn:
        conn.execute("SET session_replication_role = replica")
        with pytest.raises(psycopg.errors.RestrictViolation):
            conn.execute(statement)  # pyright: ignore[reportArgumentType, reportCallIssue]

    assert _snapshot(prov) == before


def test_the_insert_only_triggers_are_enabled_always(prov: Provisioned) -> None:
    with prov.superuser_connect(prov.state_db) as conn:
        rows = conn.execute(
            "SELECT tgname, tgenabled FROM pg_trigger "
            "WHERE tgrelid = 'audit_log.audit_events'::regclass AND NOT tgisinternal "
            "ORDER BY tgname"
        ).fetchall()

    assert rows == [
        ("audit_events_chain", "A"),
        ("audit_events_reject_delete", "A"),
        ("audit_events_reject_truncate", "A"),
        ("audit_events_reject_update", "A"),
    ]


def test_inserting_still_works_for_the_app_role(prov: Provisioned) -> None:
    with prov.as_state() as conn:
        conn.execute(
            "INSERT INTO audit_log.audit_events "
            "(actor_subject, actor_issuer, action, resource_type, resource_id, outcome) "
            "VALUES ('a', 'i', 'raw.write', 'raw', 'row-4', 'applied')"
        )

    rows, _ = _snapshot(prov)
    assert len(rows) == 4
