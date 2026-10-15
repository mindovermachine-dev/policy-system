"""`postgres_live` tests: history written before the integrity work is chained, atomically.

The upgrade path is real: a database as the previous release left it (ordinary migrations and
the `graph_gateway` migration applied, rows in `public.audit_events`) is brought forward by the
provisioning command. The expected hashes come from the independent oracle, never from the
functions under test. The atomicity test appends a failing statement to a copy of the real
migration file and applies it with the real privileged runner.

Deselected by default -- run with `uv run pytest -m postgres_live` (see
`persistence/provisioned_postgres.py`).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import psycopg
import pytest
from persistence.provisioned_postgres import Provisioned, migrate_state_database

from audit._integrity_oracle import GENESIS, canonical_v1, chain_hash
from ps_service.audit import PRIVILEGED_MIGRATIONS_DIR, PsycopgAuditStore
from ps_service.graph_gateway.provision import provision
from ps_service.persistence.errors import StatePostgresMigrationApplyError
from ps_service.persistence.migration_runner import MigrationSource
from ps_service.persistence.privileged_migration_runner import apply_privileged_migrations
from ps_service.state_migrations import GRAPH_GATEWAY_MIGRATION_SOURCE

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.postgres_live

_ISSUER = "https://issuer.example.com/"
_MIGRATION = "0001_audit_log_insert_only_hash_chain.sql"
_ROW_COUNT = 25
_TIE_INSTANT = datetime(2024, 5, 1, 12, 0, 0, 123456, tzinfo=UTC)
_ORIGINAL_COLUMNS = [
    "id",
    "occurred_at",
    "actor_subject",
    "actor_issuer",
    "action",
    "resource_type",
    "resource_id",
    "outcome",
    "details",
]


def _admin_transaction_connection(prov: Provisioned) -> psycopg.Connection[tuple[object, ...]]:
    """Open the admin connection the way the provisioning command does (not autocommit)."""
    return psycopg.connect(
        host=prov.host, port=prov.port, user=prov.superuser, dbname=prov.state_db
    )


@pytest.fixture
def old_release(fresh_provisioned: Provisioned) -> Provisioned:
    """A cluster as the previous release left it: audit rows in `public`, graph log applied."""
    prov = fresh_provisioned
    migrate_state_database(prov)
    with _admin_transaction_connection(prov) as admin:
        apply_privileged_migrations(
            admin,
            sources=[GRAPH_GATEWAY_MIGRATION_SOURCE],
            owner_role=prov.owner_role,
            app_role=prov.state_user,
        )
    return prov


def _seed_history(prov: Provisioned) -> list[uuid.UUID]:
    """Insert 25 rows as `ps_state`, 10 of them tied on `occurred_at`, ids against insert order.

    Returns the ids in the order the backfill must chain them: `(occurred_at, id)`.
    """
    base = datetime(2024, 5, 1, 9, 0, 0, tzinfo=UTC)
    rows: list[tuple[uuid.UUID, datetime]] = []
    tied_ids = [uuid.UUID(int=(0xAB << 120) + (10 - n) * 7) for n in range(10)]  # descending
    rows.extend((tied, _TIE_INSTANT) for tied in tied_ids)
    for n in range(_ROW_COUNT - 10):
        # Inserted newest first, so insertion order is the reverse of time order.
        rows.append((uuid.UUID(int=(0x10 << 120) + n), base.replace(minute=59 - n)))
    with prov.as_state() as conn:
        for position, (row_id, instant) in enumerate(rows):
            conn.execute(
                "INSERT INTO public.audit_events (id, occurred_at, actor_subject, actor_issuer, "
                "action, resource_type, resource_id, outcome, details) "
                "VALUES (%s, %s, %s, %s, 'raw.write', 'raw', %s, 'applied', %s::jsonb)",
                (
                    row_id,
                    instant,
                    f"actor-{position}",
                    _ISSUER,
                    f"target-é-{position}",
                    f'{{"n": {position}, "tag": "ü"}}',
                ),
            )
        # One row is anchored by a graph_log group, as the real graph mutation path does.
        group_id = uuid.uuid4()
        with conn.transaction():
            conn.execute(
                "INSERT INTO graph_log.groups "
                "(group_id, graph, first_position, last_position, audit_event_id) "
                "VALUES (%s, 'g', 1, 1, %s)",
                (group_id, rows[3][0]),
            )
            conn.execute(
                "INSERT INTO graph_log.entries "
                "(graph, position, group_id, name, identity, content) "
                "VALUES ('g', 1, %s, 'n', 'i', '{}'::jsonb)",
                (group_id,),
            )
    return [row_id for row_id, _ in sorted(rows, key=lambda item: (item[1], item[0]))]


def _tracking(prov: Provisioned) -> list[tuple[object, ...]]:
    with prov.as_state() as conn:
        return conn.execute(
            "SELECT component, filename FROM ps_schema_migrations ORDER BY 1, 2"
        ).fetchall()


def _snapshot_public(prov: Provisioned) -> list[tuple[object, ...]]:
    with prov.superuser_connect(prov.state_db) as conn:
        return conn.execute(
            "SELECT id, occurred_at, actor_subject, actor_issuer, action, resource_type, "
            "resource_id, outcome, details::text FROM public.audit_events ORDER BY id"
        ).fetchall()


def test_history_is_chained_in_occurred_at_then_id_order_with_the_oracle_hashes(
    old_release: Provisioned,
) -> None:
    expected_order = _seed_history(old_release)
    before = _snapshot_public(old_release)

    result = provision(old_release.provisioning_target())

    assert result.audit_integrity_applied == [_MIGRATION]
    with old_release.superuser_connect(old_release.state_db) as conn:
        rows = conn.execute(
            "SELECT chain_seq, prev_row_hash, row_hash, id, occurred_at, actor_subject, "
            "actor_issuer, action, resource_type, resource_id, outcome, details::text "
            "FROM audit_log.audit_events ORDER BY chain_seq"
        ).fetchall()
        head = conn.execute(
            "SELECT last_chain_seq, last_row_hash FROM audit_log.audit_chain_head"
        ).fetchone()
        anchored = conn.execute("SELECT count(*) FROM graph_log.groups").fetchone()
    assert [row[3] for row in rows] == expected_order
    previous = GENESIS
    for position, row in enumerate(rows, start=1):
        seq, prev_hash, row_hash, row_id, occurred_at, *strings, details_text = row
        assert (seq, prev_hash) == (position, previous)
        canonical = canonical_v1(
            chain_seq=position,
            row_id=row_id,
            occurred_at=occurred_at,  # pyright: ignore[reportArgumentType]
            strings=tuple(strings),  # pyright: ignore[reportArgumentType]
            details_text=str(details_text),
        )
        assert row_hash == chain_hash(previous, canonical)
        previous = str(row_hash)
    assert head == (_ROW_COUNT, previous)
    assert anchored == (1,)
    with old_release.superuser_connect(old_release.state_db) as conn:
        after = conn.execute(
            "SELECT id, occurred_at, actor_subject, actor_issuer, action, resource_type, "
            "resource_id, outcome, details::text FROM audit_log.audit_events ORDER BY id"
        ).fetchall()
    assert after == before


def test_a_row_written_after_the_upgrade_continues_the_backfilled_chain(
    old_release: Provisioned,
) -> None:
    _seed_history(old_release)
    provision(old_release.provisioning_target())
    with old_release.superuser_connect(old_release.state_db) as conn:
        last_hash = conn.execute(
            "SELECT row_hash FROM audit_log.audit_events WHERE chain_seq = %s", (_ROW_COUNT,)
        ).fetchone()

    PsycopgAuditStore(old_release.state_config()).record_standalone(
        actor_subject="after-upgrade",
        actor_issuer=_ISSUER,
        action="access_role.grant",
        resource_type="principal",
        resource_id="next",
        outcome="applied",
        details={"access_role": "SystemAdmin"},
    )

    with old_release.superuser_connect(old_release.state_db) as conn:
        newest = conn.execute(
            "SELECT chain_seq, prev_row_hash FROM audit_log.audit_events ORDER BY chain_seq DESC"
        ).fetchone()
    assert last_hash is not None
    assert newest == (_ROW_COUNT + 1, last_hash[0])


def test_an_empty_history_keeps_the_genesis_head(old_release: Provisioned) -> None:
    provision(old_release.provisioning_target())

    with old_release.superuser_connect(old_release.state_db) as conn:
        head = conn.execute(
            "SELECT last_chain_seq, last_row_hash FROM audit_log.audit_chain_head"
        ).fetchone()
    assert head == (0, GENESIS)


def test_the_chain_columns_are_required_once_the_history_is_chained(
    old_release: Provisioned,
) -> None:
    _seed_history(old_release)
    provision(old_release.provisioning_target())

    with old_release.superuser_connect(old_release.state_db) as conn:
        nullable = conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'audit_log' AND table_name = 'audit_events' "
            "AND column_name IN ('chain_seq', 'prev_row_hash', 'row_hash') "
            "AND is_nullable = 'YES'"
        ).fetchall()
    assert nullable == []


def test_the_backfill_precedes_every_trigger_and_the_not_null_step() -> None:
    text = (PRIVILEGED_MIGRATIONS_DIR / _MIGRATION).read_text(encoding="utf-8")

    backfill = text.index("UPDATE audit_log.audit_events")
    set_not_null = text.index("SET NOT NULL")
    first_trigger = text.index("CREATE TRIGGER")
    final_privileges = text.index("-- Final privileges")

    assert backfill < set_not_null < first_trigger < final_privileges


def test_the_backfill_uses_the_same_hash_functions_as_the_chain_trigger() -> None:
    text = (PRIVILEGED_MIGRATIONS_DIR / _MIGRATION).read_text(encoding="utf-8")
    start = text.index("UPDATE audit_log.audit_events")
    backfill_block = text[start - 1500 : start + 600]
    trigger_block = text[text.index("CREATE FUNCTION audit_log.audit_events_chain()") :]

    for block in (backfill_block, trigger_block):
        assert "audit_log.chain_hash(" in block
        assert "audit_log.canonical_row(" in block
    assert "ORDER BY occurred_at, id" in backfill_block


def _failing_copy(tmp_path: Path) -> MigrationSource:
    directory = tmp_path / "audit_integrity"
    directory.mkdir()
    real = (PRIVILEGED_MIGRATIONS_DIR / _MIGRATION).read_text(encoding="utf-8")
    (directory / _MIGRATION).write_text(real + "\nSELECT 1 / 0;\n", encoding="utf-8")
    return MigrationSource("audit_integrity", directory)


def test_a_failure_after_the_backfill_rolls_everything_back(
    old_release: Provisioned, tmp_path: Path
) -> None:
    _seed_history(old_release)
    before_rows = _snapshot_public(old_release)
    before_tracking = _tracking(old_release)

    with (
        _admin_transaction_connection(old_release) as admin,
        pytest.raises(StatePostgresMigrationApplyError) as raised,
    ):
        apply_privileged_migrations(
            admin,
            sources=[_failing_copy(tmp_path)],
            owner_role=old_release.owner_role,
            app_role=old_release.state_user,
        )

    assert _MIGRATION in str(raised.value)
    with old_release.superuser_connect(old_release.state_db) as conn:
        columns = [
            str(row[0])
            for row in conn.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = 'audit_events' "
                "ORDER BY ordinal_position"
            ).fetchall()
        ]
        assert columns == _ORIGINAL_COLUMNS
        assert conn.execute("SELECT to_regnamespace('audit_log')").fetchone() == (None,)
        assert conn.execute("SELECT to_regclass('public.audit_events')").fetchone() == (
            "audit_events",
        )
        leftovers = conn.execute(
            "SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
            "WHERE n.nspname = 'audit_log' OR p.proname LIKE 'audit_events_%chain%'"
        ).fetchone()
        triggers = conn.execute(
            "SELECT count(*) FROM pg_trigger WHERE tgrelid = 'public.audit_events'::regclass "
            "AND NOT tgisinternal"
        ).fetchone()
        owner = conn.execute(
            "SELECT pg_get_userbyid(relowner) FROM pg_class "
            "WHERE oid = 'public.audit_events'::regclass"
        ).fetchone()
    assert leftovers == (0,)
    assert triggers == (0,)
    assert owner == (old_release.state_user,)
    assert _snapshot_public(old_release) == before_rows
    assert _tracking(old_release) == before_tracking
    assert ("audit_integrity", _MIGRATION) not in _tracking(old_release)


def test_the_real_provisioning_run_succeeds_after_a_failed_attempt(
    old_release: Provisioned, tmp_path: Path
) -> None:
    _seed_history(old_release)
    with (
        _admin_transaction_connection(old_release) as admin,
        pytest.raises(StatePostgresMigrationApplyError),
    ):
        apply_privileged_migrations(
            admin,
            sources=[_failing_copy(tmp_path)],
            owner_role=old_release.owner_role,
            app_role=old_release.state_user,
        )

    result = provision(old_release.provisioning_target())

    assert result.audit_integrity_applied == [_MIGRATION]
    with old_release.superuser_connect(old_release.state_db) as conn:
        assert conn.execute("SELECT count(*) FROM audit_log.audit_events").fetchone() == (
            _ROW_COUNT,
        )


def test_the_copy_helper_really_differs_only_by_the_failing_statement(tmp_path: Path) -> None:
    source = _failing_copy(tmp_path)
    real = (PRIVILEGED_MIGRATIONS_DIR / _MIGRATION).read_text(encoding="utf-8")

    copied = (source.directory / _MIGRATION).read_text(encoding="utf-8")

    assert copied.startswith(real)
    assert copied.removeprefix(real).strip() == "SELECT 1 / 0;"
