"""Tests for `ps_service.audit.store.PsycopgAuditStore.record` (issue #147, Slice 1).

`postgres_live`-marked throughout, mirroring
`tests/authz/test_migration_runner.py`'s own marker/skip convention: whether
an `INSERT` into a real `audit_events` table actually lands correctly, and
whether an error genuinely precedes any `cur.execute` call, can only be
proven end-to-end against a real Postgres instance plus a cursor spy.

Deselected by default (BASELINE.md's tier gating) -- run explicitly with
`uv run pytest -m postgres_live` against a reachable `PS_STATE_POSTGRES_*`
instance.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

import ps_service.authz.audit_actions  # noqa: F401  # pyright: ignore[reportUnusedImport] -- side-effect import, registers access_role.* actions
from ps_service.audit import MIGRATIONS_DIR as AUDIT_MIGRATIONS_DIR
from ps_service.audit.errors import AuditInvalidDetailsError, AuditUnknownActionError
from ps_service.audit.store import AuditStore, PsycopgAuditStore
from ps_service.authz import MIGRATIONS_DIR as AUTHZ_MIGRATIONS_DIR
from ps_service.config import load_config
from ps_service.persistence import MigrationSource, apply_pending_migrations, connect_from_config

if TYPE_CHECKING:
    import psycopg
    from psycopg.rows import TupleRow


# Mirrors the source list `ps_service.main` passes to the runner at startup.
STATE_MIGRATION_SOURCES = [
    MigrationSource("audit", AUDIT_MIGRATIONS_DIR),
    MigrationSource("authz", AUTHZ_MIGRATIONS_DIR),
]


class _ExecuteSpyCursor:
    """Wraps a real cursor, recording whether `execute` was ever called.

    Proves AC-BI-005's "raises before any insert" claim directly, rather
    than inferring it from the absence of a row afterward (which a rollback
    could also explain).
    """

    def __init__(self, cur: psycopg.Cursor[TupleRow]) -> None:
        self._cur = cur
        self.execute_was_called = False

    def execute(self, query: object, params: object = None) -> None:
        """Record that `execute` was called, then delegate to the wrapped real cursor."""
        self.execute_was_called = True
        self._cur.execute(query, params)  # pyright: ignore[reportArgumentType, reportUnknownMemberType] -- delegates raw args straight to psycopg


def _require_configured_postgres() -> None:
    config = load_config()
    assert config.state_postgres_host is not None, (
        "postgres_live requires PS_STATE_POSTGRES_HOST to be set"
    )


@pytest.mark.postgres_live
def test_record_with_valid_details_inserts_exactly_one_row_with_correct_fields() -> None:
    """A valid `record` call INSERTs one row, readable back with every field intact."""
    _require_configured_postgres()
    config = load_config()
    store: AuditStore = PsycopgAuditStore(config)

    with connect_from_config(config) as conn:
        apply_pending_migrations(conn, sources=STATE_MIGRATION_SOURCES)
        with conn.cursor() as cur:
            cur.execute("DELETE FROM audit_events")  # isolate this test from prior runs' rows
            store.record(
                cur,
                actor_subject="test-actor-subject",
                actor_issuer="https://issuer.example.com/",
                action="access_role.grant",
                resource_type="principal",
                resource_id="test-target-subject",
                outcome="applied",
                details={"access_role": "SystemAdmin"},
            )
        conn.commit()

        with conn.cursor() as cur:
            cur.execute(
                "SELECT actor_subject, actor_issuer, action, resource_type, resource_id, "
                "outcome, details FROM audit_events"
            )
            rows = cur.fetchall()

    assert len(rows) == 1
    (actor_subject, actor_issuer, action, resource_type, resource_id, outcome, details) = rows[0]
    assert actor_subject == "test-actor-subject"
    assert actor_issuer == "https://issuer.example.com/"
    assert action == "access_role.grant"
    assert resource_type == "principal"
    assert resource_id == "test-target-subject"
    assert outcome == "applied"
    assert details == {"access_role": "SystemAdmin"}


@pytest.mark.postgres_live
def test_record_with_unregistered_action_raises_before_any_insert() -> None:
    """An unregistered `action` raises `AuditUnknownActionError` and never calls `cur.execute`."""
    _require_configured_postgres()
    config = load_config()
    store: AuditStore = PsycopgAuditStore(config)

    with connect_from_config(config) as conn:
        apply_pending_migrations(conn, sources=STATE_MIGRATION_SOURCES)
        with conn.cursor() as real_cur:
            spy_cur = _ExecuteSpyCursor(real_cur)
            with pytest.raises(AuditUnknownActionError):
                store.record(
                    spy_cur,  # pyright: ignore[reportArgumentType] -- structurally cursor-shaped spy, not a psycopg.Cursor subclass
                    actor_subject="test-actor-subject",
                    actor_issuer="https://issuer.example.com/",
                    action="nonexistent.action.never_registered",
                    resource_type="principal",
                    resource_id="test-target-subject",
                    outcome="applied",
                    details={},
                )
        conn.rollback()

    assert spy_cur.execute_was_called is False


@pytest.mark.postgres_live
def test_record_with_undeclared_extra_field_in_details_raises_before_any_insert() -> None:
    """A `details` payload with an extra, undeclared field raises `AuditInvalidDetailsError`."""
    _require_configured_postgres()
    config = load_config()
    store: AuditStore = PsycopgAuditStore(config)

    with connect_from_config(config) as conn:
        apply_pending_migrations(conn, sources=STATE_MIGRATION_SOURCES)
        with conn.cursor() as real_cur:
            spy_cur = _ExecuteSpyCursor(real_cur)
            with pytest.raises(AuditInvalidDetailsError):
                store.record(
                    spy_cur,  # pyright: ignore[reportArgumentType] -- structurally cursor-shaped spy, not a psycopg.Cursor subclass
                    actor_subject="test-actor-subject",
                    actor_issuer="https://issuer.example.com/",
                    action="access_role.grant",
                    resource_type="principal",
                    resource_id="test-target-subject",
                    outcome="applied",
                    details={"access_role": "SystemAdmin", "token": "should-not-be-allowed"},
                )
        conn.rollback()

    assert spy_cur.execute_was_called is False


@pytest.mark.postgres_live
def test_record_with_details_missing_a_required_field_raises_before_any_insert() -> None:
    """A `details` payload missing a required declared field raises `AuditInvalidDetailsError`."""
    _require_configured_postgres()
    config = load_config()
    store: AuditStore = PsycopgAuditStore(config)

    with connect_from_config(config) as conn:
        apply_pending_migrations(conn, sources=STATE_MIGRATION_SOURCES)
        with conn.cursor() as real_cur:
            spy_cur = _ExecuteSpyCursor(real_cur)
            with pytest.raises(AuditInvalidDetailsError):
                store.record(
                    spy_cur,  # pyright: ignore[reportArgumentType] -- structurally cursor-shaped spy, not a psycopg.Cursor subclass
                    actor_subject="test-actor-subject",
                    actor_issuer="https://issuer.example.com/",
                    action="access_role.grant",
                    resource_type="principal",
                    resource_id="test-target-subject",
                    outcome="applied",
                    details={},  # access_role is required, not defaulted
                )
        conn.rollback()

    assert spy_cur.execute_was_called is False


@pytest.mark.postgres_live
def test_a_rolled_back_transaction_after_record_leaves_no_partial_row() -> None:
    """A caller that rolls back after `record` leaves the table exactly as it was before.

    `record` never commits or rolls back its own transaction (store-level
    application of AC-BI-004's rollback principle) -- proves the surrounding
    transaction, not `record` itself, owns commit/rollback, which is exactly
    what lets Slice 2's real callers join `record`'s insert into their own
    grant/revoke transaction.
    """
    _require_configured_postgres()
    config = load_config()
    store: AuditStore = PsycopgAuditStore(config)

    with connect_from_config(config) as conn:
        apply_pending_migrations(conn, sources=STATE_MIGRATION_SOURCES)
        with conn.cursor() as cur:
            cur.execute("DELETE FROM audit_events")
        conn.commit()

        with conn.cursor() as cur:
            store.record(
                cur,
                actor_subject="test-actor-subject",
                actor_issuer="https://issuer.example.com/",
                action="access_role.grant",
                resource_type="principal",
                resource_id="test-target-subject",
                outcome="applied",
                details={"access_role": "SystemAdmin"},
            )
        # Simulate the surrounding transaction failing after `record`'s own
        # INSERT (e.g. a sibling state-change insert erroring downstream) --
        # the caller rolls back instead of committing.
        conn.rollback()

        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM audit_events")
            record = cur.fetchone()

    assert record is not None
    assert record[0] == 0
