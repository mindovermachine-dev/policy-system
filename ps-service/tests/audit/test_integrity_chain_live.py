"""`postgres_live` tests: every writer of `audit_log.audit_events` is chained by the database.

The chain trigger runs on every insert, whichever code wrote it, and overwrites any chain column
the caller supplied. Each test uses a brand-new, fully provisioned cluster (the table can never
be emptied), the real writers (`PsycopgAuditStore.record_standalone`, `record` inside a real
`PsycopgAccessRoleStore.grant`, `record_system_audit`) and raw SQL as the application role.

Deselected by default -- run with `uv run pytest -m postgres_live` (see
`persistence/provisioned_postgres.py`).
"""

from __future__ import annotations

import dataclasses
import uuid
from typing import TYPE_CHECKING

import psycopg
import pytest
from authz._fakes import FakeAccessRoleStore
from persistence.provisioned_postgres import (
    Provisioned,
    migrate_state_database,
    provision_graph_log,
)

import ps_service.authz.audit_actions  # noqa: F401  # pyright: ignore[reportUnusedImport] -- side-effect import, registers access_role.* actions
from audit._integrity_oracle import GENESIS, canonical_v1, chain_hash
from ps_service.audit import PsycopgAuditStore
from ps_service.audit.models import AuditQueryFilters
from ps_service.authz.models import AccessRole
from ps_service.authz.service import list_audit_events
from ps_service.authz.store import PsycopgAccessRoleStore
from ps_service.graph_gateway.system_audit import build_system_audit_recorder
from ps_service.persistence import connect_from_config

if TYPE_CHECKING:
    from collections.abc import Sequence

pytestmark = pytest.mark.postgres_live

_ISSUER = "https://issuer.example.com/"
_INSERT_RAW = (
    "INSERT INTO audit_log.audit_events "
    "(actor_subject, actor_issuer, action, resource_type, resource_id, outcome, details) "
    "VALUES (%s, %s, 'raw.write', 'raw', %s, 'applied', '{\"k\": [1, 2]}')"
)
_CHAIN_COLUMNS = "chain_seq, prev_row_hash, row_hash"


@pytest.fixture
def prov(fresh_provisioned: Provisioned) -> Provisioned:
    """A brand-new cluster as a started service then the provisioning command leave it."""
    migrate_state_database(fresh_provisioned)
    provision_graph_log(fresh_provisioned)
    return fresh_provisioned


def _chained_rows(prov: Provisioned) -> list[tuple[object, ...]]:
    """Every audit row, ordered by `chain_seq`, with its stored chain columns (as superuser)."""
    with prov.superuser_connect(prov.state_db) as conn:
        return conn.execute(
            "SELECT chain_seq, prev_row_hash, row_hash, id, occurred_at, actor_subject, "
            "actor_issuer, action, resource_type, resource_id, outcome, details::text "
            "FROM audit_log.audit_events ORDER BY chain_seq"
        ).fetchall()


def _head(prov: Provisioned) -> tuple[object, ...]:
    with prov.superuser_connect(prov.state_db) as conn:
        row = conn.execute(
            "SELECT last_chain_seq, last_row_hash FROM audit_log.audit_chain_head"
        ).fetchone()
    assert row is not None
    return row


def _assert_chain_matches_the_oracle(rows: Sequence[tuple[object, ...]]) -> None:
    previous = GENESIS
    for position, row in enumerate(rows, start=1):
        seq, prev_hash, row_hash, row_id, occurred_at, *strings, details_text = row
        assert seq == position
        assert prev_hash == previous
        canonical = canonical_v1(
            chain_seq=position,
            row_id=row_id,
            occurred_at=occurred_at,  # pyright: ignore[reportArgumentType]
            strings=tuple(strings),  # pyright: ignore[reportArgumentType]
            details_text=str(details_text),
        )
        assert row_hash == chain_hash(previous, canonical)
        previous = str(row_hash)


def _standalone(store: PsycopgAuditStore, resource_id: str) -> None:
    store.record_standalone(
        actor_subject="standalone-actor",
        actor_issuer=_ISSUER,
        action="access_role.grant",
        resource_type="principal",
        resource_id=resource_id,
        outcome="applied",
        details={"access_role": "SystemAdmin"},
    )


def test_the_first_row_chains_to_the_genesis_hash_and_sets_the_head(prov: Provisioned) -> None:
    assert _head(prov) == (0, GENESIS)

    _standalone(PsycopgAuditStore(prov.state_config()), "first-target")

    rows = _chained_rows(prov)
    assert len(rows) == 1
    assert rows[0][0] == 1
    assert rows[0][1] == GENESIS
    _assert_chain_matches_the_oracle(rows)
    assert _head(prov) == (1, rows[0][2])


def test_every_real_writer_and_a_raw_insert_extend_one_chain(prov: Provisioned) -> None:
    config = prov.state_config()
    audit_store = PsycopgAuditStore(config)
    _standalone(audit_store, "standalone-target")
    PsycopgAccessRoleStore(config, audit_store=audit_store).grant(
        actor=("grant-actor", _ISSUER),
        target=("grant-target", _ISSUER),
        access_role=AccessRole.SYSTEM_ADMIN,
    )
    build_system_audit_recorder(config)(
        action="ingestion.system", resource_type="run", resource_id="sys-1", details={"n": 1}
    )
    with prov.as_state() as conn:
        conn.execute(_INSERT_RAW, ("raw-actor", _ISSUER, "raw-target"))  # pyright: ignore[reportArgumentType]

    rows = _chained_rows(prov)

    assert [row[7] for row in rows] == [
        "access_role.grant",
        "access_role.grant",
        "ingestion.system",
        "raw.write",
    ]
    _assert_chain_matches_the_oracle(rows)
    assert _head(prov) == (4, rows[-1][2])


def test_a_direct_record_call_inside_a_caller_transaction_is_chained(prov: Provisioned) -> None:
    config = prov.state_config()
    with connect_from_config(config) as conn, conn.cursor() as cur:
        PsycopgAuditStore(config).record(
            cur,
            actor_subject="direct-actor",
            actor_issuer=_ISSUER,
            action="access_role.grant",
            resource_type="principal",
            resource_id="direct-target",
            outcome="applied",
            details={"access_role": "SystemAdmin"},
        )
        conn.commit()

    _assert_chain_matches_the_oracle(_chained_rows(prov))


def test_caller_supplied_chain_values_are_overwritten(prov: Provisioned) -> None:
    with prov.as_state() as conn:
        conn.execute(
            "INSERT INTO audit_log.audit_events "
            "(actor_subject, actor_issuer, action, resource_type, resource_id, outcome, "
            "chain_seq, prev_row_hash, row_hash) "
            "VALUES ('forger', 'i', 'raw.write', 'raw', 'forged', 'applied', 999, 'x', 'y')"
        )

    rows = _chained_rows(prov)

    assert [row[0] for row in rows] == [1]
    _assert_chain_matches_the_oracle(rows)
    assert _head(prov) == (1, rows[0][2])


def test_a_multi_row_insert_chains_every_row_in_order(prov: Provisioned) -> None:
    with prov.as_state() as conn:
        conn.execute(
            "INSERT INTO audit_log.audit_events "
            "(actor_subject, actor_issuer, action, resource_type, resource_id, outcome) "
            "SELECT 'bulk', 'i', 'raw.write', 'raw', 'row-' || n, 'applied' "
            "FROM generate_series(1, 25) AS n"
        )

    rows = _chained_rows(prov)

    assert len(rows) == 25
    _assert_chain_matches_the_oracle(rows)
    assert _head(prov) == (25, rows[-1][2])


def test_the_app_role_cannot_touch_the_head_table_yet_inserting_works(prov: Provisioned) -> None:
    for statement in (
        "SELECT * FROM audit_log.audit_chain_head",
        "SELECT * FROM audit_log.audit_chain_head FOR UPDATE",
        "UPDATE audit_log.audit_chain_head SET last_chain_seq = 0",
        "DELETE FROM audit_log.audit_chain_head",
        "INSERT INTO audit_log.audit_chain_head VALUES (true, 5, repeat('a', 64))",
        "TRUNCATE audit_log.audit_chain_head",
    ):
        with prov.as_state() as conn, pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute(statement)  # pyright: ignore[reportArgumentType]

    with prov.as_state() as conn:
        conn.execute(_INSERT_RAW, ("app-role", _ISSUER, "works"))  # pyright: ignore[reportArgumentType]

    assert len(_chained_rows(prov)) == 1


def test_a_rolled_back_insert_leaves_the_head_unchanged_and_the_number_is_reused(
    prov: Provisioned,
) -> None:
    with prov.as_state() as conn:
        conn.execute(_INSERT_RAW, ("kept", _ISSUER, "kept"))  # pyright: ignore[reportArgumentType]
    head_before = _head(prov)

    with prov.as_state() as conn:
        conn.autocommit = False
        conn.execute(_INSERT_RAW, ("dropped", _ISSUER, "dropped"))  # pyright: ignore[reportArgumentType]
        conn.rollback()
    assert _head(prov) == head_before

    with prov.as_state() as conn:
        conn.execute(_INSERT_RAW, ("next", _ISSUER, "next"))  # pyright: ignore[reportArgumentType]

    rows = _chained_rows(prov)
    assert [row[0] for row in rows] == [1, 2]
    _assert_chain_matches_the_oracle(rows)


def test_list_audit_events_output_keys_are_unchanged_by_the_chain_columns(
    prov: Provisioned,
) -> None:
    owner = ("owner-subject", _ISSUER)
    roles = FakeAccessRoleStore(expected_owner=owner)
    roles.bootstrap_first_owner(owner)
    audit_store = PsycopgAuditStore(prov.state_config())
    _standalone(audit_store, f"keys-{uuid.uuid4().hex}")

    page = list_audit_events(
        owner,
        filters=AuditQueryFilters(),
        cursor=None,
        page_size=10,
        access_role_store=roles,
        audit_store=audit_store,
    )

    (event,) = page.events
    assert {field.name for field in dataclasses.fields(event)} == {
        "id",
        "occurred_at",
        "actor_subject",
        "actor_issuer",
        "action",
        "resource_type",
        "resource_id",
        "outcome",
        "details",
    }
