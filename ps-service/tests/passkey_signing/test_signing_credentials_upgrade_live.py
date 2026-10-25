"""Live proof that upgrading the passkey signing migrations to the latest file keeps stored data.

Applies the first two passkey migrations, stores one signing credential, then applies the rest
and checks that exactly the later file ran, the stored credential survived with an empty
`rp_id`, and the index the later file adds now exists.

Deselected by default; select with
`uv run pytest -m postgres_live
ps-service/tests/passkey_signing/test_signing_credentials_upgrade_live.py`.

Start a scratch Postgres:
`podman run -d --name <name> -p 54329:5432 -e POSTGRES_HOST_AUTH_METHOD=trust postgres:16-alpine`
and export `PS_STATE_POSTGRES_HOST`, `PS_STATE_POSTGRES_PORT`, `PS_STATE_POSTGRES_DATABASE`
and `PS_STATE_POSTGRES_USER` (e.g. `localhost`, `54329`, `postgres`, `postgres`). Only these are
needed, not `PS_PASSKEYSIGNING_POSTGRES_*`. For the whole `-m postgres_live` suite also export
`PS_TEST_POSTGRES_SUPERUSER_DSN` (e.g. `postgresql://postgres@localhost:54329/postgres`) with
`psql` on `PATH`, because neighbouring modules read it. `-m postgres_live` runs in a single
process.

Each test works in its own uuid-named schema, dropped on teardown.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, NamedTuple

import pytest
from persistence.live_schema_support import (
    apply_passkey_directory,
    column_shapes,
    copy_migration_prefix,
    index_definitions,
    isolated_schema,
    passkey_migrations_dir,
    require_configured_state_postgres,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from psycopg import Connection
    from psycopg.rows import TupleRow

pytestmark = pytest.mark.postgres_live

TABLE = "signing_credentials"
SELECT_CREDENTIAL = (
    "SELECT id, actor_subject, actor_issuer, credential_id, public_key, sign_count, created_at "
    "FROM signing_credentials"
)


class Upgrade(NamedTuple):
    """The state around a passkey upgrade from the first two migrations to the full set."""

    conn: Connection[TupleRow]
    columns_before: set[str]
    applied_first: list[str]
    applied_later: list[str]
    credential_before: tuple[object, ...]
    credential_after: tuple[object, ...]
    rp_id_after: object


def _read_credential(conn: Connection[TupleRow]) -> tuple[object, ...]:
    with conn.cursor() as cur:
        cur.execute(SELECT_CREDENTIAL)
        row = cur.fetchone()
    assert row is not None
    return row


@pytest.fixture
def upgrade(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Upgrade]:
    require_configured_state_postgres()
    real_dir = passkey_migrations_dir()
    first_two = copy_migration_prefix(real_dir, 2, tmp_path / "first_two")
    with isolated_schema() as (conn, _schema):
        applied_first = apply_passkey_directory(conn, first_two, monkeypatch)
        columns_before = set(column_shapes(conn, TABLE))
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO signing_credentials "
                "(actor_subject, actor_issuer, credential_id, public_key) VALUES "
                "('upgrade-actor', 'https://issuer.example.com/', '\\x0102'::bytea, "
                "'\\x0304'::bytea)"
            )
        conn.commit()
        credential_before = _read_credential(conn)
        applied_later = apply_passkey_directory(conn, real_dir, monkeypatch)
        with conn.cursor() as cur:
            cur.execute("SELECT rp_id FROM signing_credentials")
            rp_row = cur.fetchone()
        assert rp_row is not None
        credential_after = _read_credential(conn)
        yield Upgrade(
            conn,
            columns_before,
            applied_first,
            applied_later,
            credential_before,
            credential_after,
            rp_row[0],
        )


def test_first_two_migrations_apply_without_the_later_one(upgrade: Upgrade) -> None:
    assert upgrade.applied_first == [
        "0001_pending_approvals.sql",
        "0002_signing_credentials.sql",
    ]


def test_credentials_have_no_rp_id_column_before_the_upgrade(upgrade: Upgrade) -> None:
    assert "rp_id" not in upgrade.columns_before


def test_upgrade_applies_only_the_later_migration(upgrade: Upgrade) -> None:
    assert upgrade.applied_later == ["0003_signing_credentials_rp_id.sql"]


def test_upgrade_keeps_the_stored_credential_columns_unchanged(upgrade: Upgrade) -> None:
    assert upgrade.credential_after == upgrade.credential_before


def test_upgrade_leaves_the_existing_credential_rp_id_empty(upgrade: Upgrade) -> None:
    assert upgrade.rp_id_after is None


def test_upgrade_adds_the_actor_and_rp_index(upgrade: Upgrade) -> None:
    assert "signing_credentials_actor_rp_idx" in index_definitions(upgrade.conn, TABLE)
