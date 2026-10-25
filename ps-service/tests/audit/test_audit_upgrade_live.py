"""Live proof that upgrading the audit migrations from the first file to the full set is safe.

Applies only the first audit migration, stores one event, then applies the rest and checks that
exactly the later file ran, the details indexes it adds now exist, and the stored event is
unchanged.

Deselected by default; select with
`uv run pytest -m postgres_live ps-service/tests/audit/test_audit_upgrade_live.py`.

Start a scratch Postgres:
`podman run -d --name <name> -p 54329:5432 -e POSTGRES_HOST_AUTH_METHOD=trust postgres:16-alpine`
and export `PS_STATE_POSTGRES_HOST`, `PS_STATE_POSTGRES_PORT`, `PS_STATE_POSTGRES_DATABASE`
and `PS_STATE_POSTGRES_USER` (e.g. `localhost`, `54329`, `postgres`, `postgres`). For the whole
`-m postgres_live` suite also export `PS_TEST_POSTGRES_SUPERUSER_DSN` (e.g.
`postgresql://postgres@localhost:54329/postgres`) with `psql` on `PATH`, because neighbouring
modules read it. `-m postgres_live` runs in a single process.

Each test works in its own uuid-named schema, dropped on teardown.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, NamedTuple

import pytest
from persistence.live_schema_support import (
    copy_migration_prefix,
    index_definitions,
    isolated_schema,
    require_configured_state_postgres,
)

from ps_service.audit import MIGRATIONS_DIR as AUDIT_MIGRATIONS_DIR
from ps_service.persistence import MigrationSource, apply_pending_migrations

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from psycopg import Connection
    from psycopg.rows import TupleRow

pytestmark = pytest.mark.postgres_live

TABLE = "audit_events"
DETAILS_INDEXES = {
    "audit_events_details_celex_idx",
    "audit_events_details_regulatory_instrument_id_idx",
    "audit_events_details_instrument_id_idx",
}
SELECT_EVENT = (
    "SELECT id, occurred_at, actor_subject, actor_issuer, action, resource_type, resource_id, "
    "outcome, details FROM audit_events"
)


class Upgrade(NamedTuple):
    """The state around an audit upgrade from the first migration to the full set."""

    conn: Connection[TupleRow]
    indexes_before: set[str]
    applied: list[str]
    event_before: tuple[object, ...]
    event_after: tuple[object, ...]


def _read_event(conn: Connection[TupleRow]) -> tuple[object, ...]:
    with conn.cursor() as cur:
        cur.execute(SELECT_EVENT)
        row = cur.fetchone()
    assert row is not None
    return row


@pytest.fixture
def upgrade(tmp_path: Path) -> Iterator[Upgrade]:
    require_configured_state_postgres()
    first_only = copy_migration_prefix(AUDIT_MIGRATIONS_DIR, 1, tmp_path / "first_only")
    with isolated_schema() as (conn, _schema):
        apply_pending_migrations(conn, sources=[MigrationSource("audit", first_only)])
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO audit_events "
                "(actor_subject, actor_issuer, action, resource_type, resource_id, outcome, "
                "details) VALUES "
                "('upgrade-actor', 'https://issuer.example.com/', 'upgrade.action', 'principal', "
                "'upgrade-target', 'applied', '{\"key\": \"value\"}'::jsonb)"
            )
        conn.commit()
        indexes_before = set(index_definitions(conn, TABLE))
        event_before = _read_event(conn)
        applied = apply_pending_migrations(
            conn, sources=[MigrationSource("audit", AUDIT_MIGRATIONS_DIR)]
        )
        yield Upgrade(conn, indexes_before, applied, event_before, _read_event(conn))


def test_first_migration_alone_has_none_of_the_details_indexes(upgrade: Upgrade) -> None:
    assert upgrade.indexes_before.isdisjoint(DETAILS_INDEXES)


def test_upgrade_applies_only_the_later_migration(upgrade: Upgrade) -> None:
    assert upgrade.applied == ["0002_audit_events_details_indexes.sql"]


def test_upgrade_adds_the_three_details_indexes(upgrade: Upgrade) -> None:
    assert set(index_definitions(upgrade.conn, TABLE)) >= DETAILS_INDEXES


def test_upgrade_leaves_the_stored_event_unchanged(upgrade: Upgrade) -> None:
    assert upgrade.event_after == upgrade.event_before
