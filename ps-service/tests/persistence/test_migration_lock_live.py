"""`postgres_live` tests for the migration advisory lock shared by both runners (#205 follow-up).

The service (ordinary runner, `ps_state`) and the provisioning command (privileged runner, admin
credential) can run against one empty database at the same time. Both take one
transaction-scoped advisory lock around each pending file, re-check the tracking table inside
it, and bound the wait with `lock_timeout_seconds`. Each test uses the module's own database
(advisory locks are per database), unique component names and tables, and never relies on timing
to decide an outcome: races are ordered with `pg_locks` polling and `threading.Event`.

Deselected by default -- run with `uv run pytest -m postgres_live` (see `provisioned_postgres.py`).
"""

from __future__ import annotations

import threading
import time
import uuid
from typing import TYPE_CHECKING, cast

import psycopg
import pytest

from ps_service.persistence import (
    MigrationSource,
    StatePostgresMigrationApplyError,
    StatePostgresMigrationLockError,
    apply_pending_migrations,
    connect_from_config,
)
from ps_service.persistence.migration_runner import MIGRATION_LOCK_KEY
from ps_service.persistence.privileged_migration_runner import apply_privileged_migrations

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path
    from typing import LiteralString

    from psycopg.rows import TupleRow

    from persistence.provisioned_postgres import Provisioned

pytestmark = pytest.mark.postgres_live

_WAIT_SECONDS = 20.0
_SHORT_TIMEOUT_SECONDS = 0.3
_LOCK_CLASSID = MIGRATION_LOCK_KEY >> 32
_LOCK_OBJID = MIGRATION_LOCK_KEY & 0xFFFFFFFF


def _new_source(tmp_path: Path) -> tuple[MigrationSource, str]:
    """Write one migration creating a uniquely named table; return its source and table name."""
    table = f"lock_t_{uuid.uuid4().hex[:10]}"
    directory = tmp_path / "migrations"
    directory.mkdir()
    (directory / "0001_create.sql").write_text(f"CREATE TABLE {table} (id int)", encoding="utf-8")
    return MigrationSource(f"lock_{uuid.uuid4().hex[:8]}", directory), table


def _scalar(conn: psycopg.Connection[TupleRow], query: str, *params: object) -> object:
    row = conn.execute(query, params).fetchone()  # pyright: ignore[reportArgumentType]  # test-only dynamic SQL
    assert row is not None
    return row[0]


def _waiting_for_lock(prov: Provisioned) -> bool:
    """Whether some session is blocked waiting for the migration lock (polled, never slept on)."""
    with prov.superuser_connect(prov.state_db) as conn:
        waiting = _scalar(
            conn,
            "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND NOT granted "
            "AND classid = %s AND objid = %s",
            _LOCK_CLASSID,
            _LOCK_OBJID,
        )
    return isinstance(waiting, int) and waiting > 0


def _wait_until(condition: object) -> None:
    assert callable(condition)
    deadline = time.monotonic() + _WAIT_SECONDS
    while not condition():
        assert time.monotonic() < deadline, "condition not reached in time"
        time.sleep(0.05)


@pytest.fixture(name="state_conn")
def _state_conn(  # pyright: ignore[reportUnusedFunction]  # pytest fixture used by name
    provisioned: Provisioned,
) -> Iterator[psycopg.Connection[TupleRow]]:
    with connect_from_config(provisioned.state_config()) as conn:
        yield conn


@pytest.fixture(name="lock_holder")
def _lock_holder(  # pyright: ignore[reportUnusedFunction]  # pytest fixture used by name
    provisioned: Provisioned,
) -> Iterator[psycopg.Connection[TupleRow]]:
    """A second session that holds the migration lock until released by the test."""
    with provisioned.as_state() as holder:
        holder.execute("SELECT pg_advisory_lock(%s)", (MIGRATION_LOCK_KEY,))
        yield holder
        holder.execute("SELECT pg_advisory_unlock_all()")


def _hold(holder: psycopg.Connection[TupleRow]) -> None:
    holder.execute("SELECT pg_advisory_lock(%s)", (MIGRATION_LOCK_KEY,))


def _release(holder: psycopg.Connection[TupleRow]) -> None:
    holder.execute("SELECT pg_advisory_unlock(%s)", (MIGRATION_LOCK_KEY,))


def test_lock_key_is_outside_the_int32_range_of_hashtext_users() -> None:
    assert not -(2**31) <= MIGRATION_LOCK_KEY < 2**31
    assert MIGRATION_LOCK_KEY < 2**63


def test_ordinary_runner_gives_up_with_a_bounded_sanitized_error_while_the_lock_is_held(
    provisioned: Provisioned,
    state_conn: psycopg.Connection[TupleRow],
    lock_holder: psycopg.Connection[TupleRow],
    tmp_path: Path,
) -> None:
    source, table = _new_source(tmp_path)
    _release(lock_holder)
    apply_pending_migrations(state_conn, sources=[])  # the tracking table already exists
    _hold(lock_holder)

    with pytest.raises(StatePostgresMigrationApplyError, match="lock") as raised:
        apply_pending_migrations(
            state_conn, sources=[source], lock_timeout_seconds=_SHORT_TIMEOUT_SECONDS
        )

    assert f"{source.component}/0001_create.sql" in str(raised.value)
    assert "CREATE TABLE" not in str(raised.value)
    with provisioned.as_state() as check:
        assert _scalar(check, "SELECT to_regclass(%s)", table) is None
        assert (
            _scalar(
                check,
                "SELECT count(*) FROM ps_schema_migrations WHERE component = %s",
                source.component,
            )
            == 0
        )

    _release(lock_holder)
    assert apply_pending_migrations(state_conn, sources=[source]) == ["0001_create.sql"]


def test_creating_the_tracking_table_on_an_empty_database_also_respects_the_lock_timeout(
    fresh_provisioned: Provisioned, tmp_path: Path
) -> None:
    source, _ = _new_source(tmp_path)
    with (
        fresh_provisioned.as_state() as holder,
        connect_from_config(fresh_provisioned.state_config()) as conn,
    ):
        holder.execute("SELECT pg_advisory_lock(%s)", (MIGRATION_LOCK_KEY,))

        with pytest.raises(StatePostgresMigrationLockError, match="tracking table"):
            apply_pending_migrations(
                conn, sources=[source], lock_timeout_seconds=_SHORT_TIMEOUT_SECONDS
            )

        _release(holder)
        assert apply_pending_migrations(conn, sources=[source]) == ["0001_create.sql"]


def test_runner_with_nothing_pending_never_waits_for_the_lock(
    state_conn: psycopg.Connection[TupleRow],
    lock_holder: psycopg.Connection[TupleRow],
    tmp_path: Path,
) -> None:
    source, _ = _new_source(tmp_path)
    _release(lock_holder)
    apply_pending_migrations(state_conn, sources=[source])
    _hold(lock_holder)

    applied = apply_pending_migrations(
        state_conn, sources=[source], lock_timeout_seconds=_SHORT_TIMEOUT_SECONDS
    )

    assert applied == []


def test_loser_re_reads_inside_the_lock_and_applies_nothing(
    provisioned: Provisioned,
    state_conn: psycopg.Connection[TupleRow],
    tmp_path: Path,
) -> None:
    source, table = _new_source(tmp_path)
    apply_pending_migrations(state_conn, sources=[])  # tracking table exists, file is pending
    outcome: list[list[str]] = []
    errors: list[BaseException] = []

    def loser() -> None:
        try:
            with connect_from_config(provisioned.state_config()) as conn:
                outcome.append(
                    apply_pending_migrations(
                        conn, sources=[source], lock_timeout_seconds=_WAIT_SECONDS
                    )
                )
        except BaseException as exc:  # noqa: BLE001  # surfaced to the test thread below
            errors.append(exc)

    with provisioned.as_state() as winner:
        with winner.transaction():
            winner.execute("SELECT pg_advisory_xact_lock(%s)", (MIGRATION_LOCK_KEY,))
            thread = threading.Thread(target=loser)
            thread.start()
            _wait_until(lambda: _waiting_for_lock(provisioned))
            winner.execute(cast("LiteralString", f"CREATE TABLE {table} (id int)"))
            winner.execute(
                "INSERT INTO ps_schema_migrations (component, filename) VALUES (%s, %s)",
                (source.component, "0001_create.sql"),
            )
        thread.join(timeout=_WAIT_SECONDS)

    assert not thread.is_alive()
    assert errors == []
    assert outcome == [[]]


def test_privileged_runner_takes_the_same_lock_and_gives_up_while_it_is_held(
    provisioned: Provisioned,
    lock_holder: psycopg.Connection[TupleRow],
    tmp_path: Path,
) -> None:
    source, table = _new_source(tmp_path)

    with psycopg.connect(
        host=provisioned.host,
        port=provisioned.port,
        user=provisioned.superuser,
        dbname=provisioned.state_db,
    ) as admin:
        with pytest.raises(StatePostgresMigrationApplyError, match="lock"):
            apply_privileged_migrations(
                admin,
                sources=[source],
                owner_role=provisioned.owner_role,
                app_role=provisioned.state_user,
                lock_timeout_seconds=_SHORT_TIMEOUT_SECONDS,
            )
        _release(lock_holder)
        applied = apply_privileged_migrations(
            admin,
            sources=[source],
            owner_role=provisioned.owner_role,
            app_role=provisioned.state_user,
        )

    assert applied == ["0001_create.sql"]
    with provisioned.superuser_connect(provisioned.state_db) as check:
        assert _scalar(check, "SELECT to_regclass(%s) IS NOT NULL", table) is True
