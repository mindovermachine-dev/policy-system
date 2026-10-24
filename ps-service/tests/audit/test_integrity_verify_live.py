"""`postgres_live` tests: `audit_log.verify_chain()` and `verify-audit-log`, end to end.

Every test provisions a brand-new cluster (the audit table can never be emptied), writes rows
with the real writers, and verifies through the application role exactly as the service does:
`PsycopgAuditStore.verify_integrity` runs the SQL function in a REPEATABLE READ read-only
snapshot. The tool test goes MCP tool -> use case -> real audit store -> SQL function and back.

Deselected by default -- run with `uv run pytest -m postgres_live` (see
`persistence/provisioned_postgres.py`).
"""

from __future__ import annotations

import asyncio
import json
from contextlib import contextmanager
from typing import TYPE_CHECKING, cast

import psycopg
import pytest
from authz._fakes import FakeAccessRoleStore
from mcp.server.auth.middleware.auth_context import auth_context_var
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.provider import AccessToken
from mcp.types import CallToolResult, TextContent
from mcp_interface._environment import build_test_server
from persistence.provisioned_postgres import (
    Provisioned,
    migrate_state_database,
    provision_graph_log,
)

from audit._integrity_support import assert_chain_is_intact
from ps_service.audit import PsycopgAuditStore
from ps_service.audit.models import AuditChainBreak, AuditChainVerification
from ps_service.logging import configure, reset_for_tests

if TYPE_CHECKING:
    from collections.abc import Callable, Generator
    from datetime import datetime

pytestmark = pytest.mark.postgres_live

_ISSUER = "https://issuer.example.com/"
_OWNER = ("verify-owner", _ISSUER)
_RAW = (
    "INSERT INTO audit_log.audit_events "
    "(actor_subject, actor_issuer, action, resource_type, resource_id, outcome, details) "
    "VALUES (%s, %s, 'raw.write', 'raw', %s, 'applied', '{\"k\": [1, 2]}')"
)


@pytest.fixture
def prov(fresh_provisioned: Provisioned) -> Provisioned:
    """A brand-new cluster as a started service then the provisioning command leave it."""
    migrate_state_database(fresh_provisioned)
    provision_graph_log(fresh_provisioned)
    return fresh_provisioned


def _write_rows(prov: Provisioned, count: int) -> None:
    store = PsycopgAuditStore(prov.state_config())
    for n in range(count):
        store.record_standalone(
            actor_subject=f"actor-{n}",
            actor_issuer=_ISSUER,
            action="instrument.restore",
            resource_type="instrument",
            resource_id=f"CRA-{n}",
            outcome="applied",
            details={"instrument_id": f"CRA-{n}", "status": "started", "source": "catalog"},
        )


def test_an_empty_chain_is_intact_with_nothing_checked(prov: Provisioned) -> None:
    result = PsycopgAuditStore(prov.state_config()).verify_integrity()

    assert result == AuditChainVerification(
        intact=True, rows_checked=0, head_chain_seq=0, first_break=None
    )


def test_a_chain_written_by_the_real_writers_is_intact_from_genesis_to_head(
    prov: Provisioned,
) -> None:
    _write_rows(prov, 6)
    with prov.as_state() as conn:
        conn.execute(_RAW, ("raw-actor", _ISSUER, "raw-1"))

    result = PsycopgAuditStore(prov.state_config()).verify_integrity()

    assert result == AuditChainVerification(
        intact=True, rows_checked=7, head_chain_seq=7, first_break=None
    )
    assert assert_chain_is_intact(prov) == 7  # the independent oracle agrees


def test_verification_writes_nothing(prov: Provisioned) -> None:
    _write_rows(prov, 3)
    with prov.superuser_connect(prov.state_db) as conn:
        before = conn.execute("SELECT count(*) FROM audit_log.audit_events").fetchone()

    PsycopgAuditStore(prov.state_config()).verify_integrity()

    with prov.superuser_connect(prov.state_db) as conn:
        assert conn.execute("SELECT count(*) FROM audit_log.audit_events").fetchone() == before


def test_the_function_refuses_to_run_below_repeatable_read(prov: Provisioned) -> None:
    """A READ COMMITTED call could see a head and rows from two different instants."""
    with prov.as_state() as conn, pytest.raises(psycopg.errors.InvalidTransactionState):
        conn.execute("SELECT * FROM audit_log.verify_chain()")


def test_the_function_is_callable_by_the_application_role_only_in_a_snapshot(
    prov: Provisioned,
) -> None:
    _write_rows(prov, 2)
    with prov.as_state() as conn:
        conn.autocommit = False
        conn.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
        row = conn.execute("SELECT intact, rows_checked FROM audit_log.verify_chain()").fetchone()

    assert row == (True, 2)


def _verified_actor() -> AccessToken:
    return AccessToken(
        token="test-token",
        client_id="test-client",
        scopes=[],
        subject=_OWNER[0],
        claims={"iss": _ISSUER},
    )


_OVERRIDES: dict[str, object] = {"audit_store_factory": PsycopgAuditStore}


def _call_tool() -> CallToolResult:
    token = auth_context_var.set(AuthenticatedUser(_verified_actor()))
    try:
        result = asyncio.run(build_test_server(**_OVERRIDES).call_tool("verify-audit-log", {}))
    finally:
        auth_context_var.reset(token)
    assert isinstance(result, CallToolResult)
    return result


def _factory(store: object) -> Callable[..., object]:
    def _make(_config: object, **_kwargs: object) -> object:
        return store

    return _make


def test_the_tool_reports_the_intact_chain_through_the_real_store_and_sql(
    fresh_state_postgres_env: Provisioned, monkeypatch: pytest.MonkeyPatch
) -> None:
    configure()
    prov = fresh_state_postgres_env
    _write_rows(prov, 4)
    roles = FakeAccessRoleStore(expected_owner=_OWNER)
    monkeypatch.setitem(_OVERRIDES, "role_store_factory", _factory(roles))

    result = _call_tool()

    reset_for_tests()
    block = result.content[0]
    assert isinstance(block, TextContent)
    assert json.loads(block.text) == {
        "intact": True,
        "rows_checked": 4,
        "head_chain_seq": 4,
        "first_break": None,
    }


def test_rows_chained_by_the_backfill_verify_like_new_rows(
    fresh_provisioned: Provisioned,
) -> None:
    """History written before the integrity work is chained by the migration, then extended."""
    prov = fresh_provisioned
    migrate_state_database(prov)  # the previous release: audit_events still in `public`
    with prov.as_state() as conn:
        for n in range(3):
            conn.execute(
                "INSERT INTO public.audit_events (actor_subject, actor_issuer, action, "
                "resource_type, resource_id, outcome, details) "
                "VALUES (%s, %s, 'raw.write', 'raw', %s, 'applied', '{}'::jsonb)",
                (f"old-{n}", _ISSUER, f"old-{n}"),
            )
    provision_graph_log(prov)
    _write_rows(prov, 2)

    result = PsycopgAuditStore(prov.state_config()).verify_integrity()

    assert result == AuditChainVerification(
        intact=True, rows_checked=5, head_chain_seq=5, first_break=None
    )


# --- tamper detection -------------------------------------------------------------
#
# Only a superuser can alter the trail, and only after switching the (enabled-always) triggers
# off. Each scenario builds ten chained rows, tampers that way, switches the triggers back on and
# verifies through the application role.

_ROWS = 10
_TABLES = ("audit_log.audit_events", "audit_log.audit_chain_head")


@pytest.fixture
def ten_rows(prov: Provisioned) -> Provisioned:
    _write_rows(prov, _ROWS)
    return prov


@contextmanager
def _tampering(prov: Provisioned) -> Generator[psycopg.Connection[tuple[object, ...]]]:
    """A superuser transaction with every trigger off on both audit tables, then back on."""
    with prov.superuser_connect(prov.state_db) as conn:
        conn.autocommit = False
        for table in _TABLES:
            conn.execute(f"ALTER TABLE {table} DISABLE TRIGGER ALL")  # type: ignore[arg-type]
        yield conn
        for table in _TABLES:
            conn.execute(f"ALTER TABLE {table} ENABLE TRIGGER ALL")  # type: ignore[arg-type]
        conn.commit()


def _row_identity(prov: Provisioned, chain_seq: int) -> tuple[str, datetime]:
    with prov.superuser_connect(prov.state_db) as conn:
        row = conn.execute(
            "SELECT id::text, occurred_at FROM audit_log.audit_events WHERE chain_seq = %s",
            (chain_seq,),
        ).fetchone()
    assert row is not None
    return str(row[0]), cast("datetime", row[1])


def _verify(prov: Provisioned) -> AuditChainVerification:
    return PsycopgAuditStore(prov.state_config()).verify_integrity()


def _assert_row_break(
    prov: Provisioned, result: AuditChainVerification, *, kind: str, chain_seq: int, rows: int
) -> None:
    event_id, occurred_at = _row_identity(prov, chain_seq)
    assert result.intact is False
    assert result.rows_checked == rows
    assert result.first_break == AuditChainBreak(
        kind=kind,  # type: ignore[arg-type]
        chain_seq=chain_seq,
        event_id=event_id,
        occurred_at=occurred_at,
    )


def test_an_altered_row_is_reported_as_a_row_hash_mismatch_with_its_id_and_time(
    ten_rows: Provisioned,
) -> None:
    prov = ten_rows
    with _tampering(prov) as conn:
        conn.execute(
            "UPDATE audit_log.audit_events SET details = '{\"tampered\": true}'::jsonb "
            "WHERE chain_seq = 4"
        )

    _assert_row_break(prov, _verify(prov), kind="row_hash_mismatch", chain_seq=4, rows=10)


def test_with_two_altered_rows_the_lowest_sequence_is_reported(ten_rows: Provisioned) -> None:
    prov = ten_rows
    with _tampering(prov) as conn:
        conn.execute("UPDATE audit_log.audit_events SET action = 'x.y' WHERE chain_seq IN (7, 4)")

    _assert_row_break(prov, _verify(prov), kind="row_hash_mismatch", chain_seq=4, rows=10)


def test_a_rewritten_link_is_reported_as_a_prev_hash_mismatch(ten_rows: Provisioned) -> None:
    prov = ten_rows
    with _tampering(prov) as conn:
        conn.execute(
            "UPDATE audit_log.audit_events SET prev_row_hash = repeat('a', 64) WHERE chain_seq = 5"
        )

    _assert_row_break(prov, _verify(prov), kind="prev_hash_mismatch", chain_seq=5, rows=10)


def test_a_deleted_middle_row_is_reported_as_a_gap_at_the_first_row_after_it(
    ten_rows: Provisioned,
) -> None:
    prov = ten_rows
    with _tampering(prov) as conn:
        conn.execute("DELETE FROM audit_log.audit_events WHERE chain_seq = 6")

    _assert_row_break(prov, _verify(prov), kind="sequence_gap", chain_seq=7, rows=9)


def test_a_deleted_first_row_is_reported_as_a_gap_at_the_new_first_row(
    ten_rows: Provisioned,
) -> None:
    prov = ten_rows
    with _tampering(prov) as conn:
        conn.execute("DELETE FROM audit_log.audit_events WHERE chain_seq = 1")

    _assert_row_break(prov, _verify(prov), kind="sequence_gap", chain_seq=2, rows=9)


def test_swapped_timestamps_are_reported_at_the_lower_row(ten_rows: Provisioned) -> None:
    prov = ten_rows
    with _tampering(prov) as conn:
        conn.execute(
            "UPDATE audit_log.audit_events t SET occurred_at = o.occurred_at "
            "FROM (SELECT chain_seq, occurred_at FROM audit_log.audit_events "
            "WHERE chain_seq IN (2, 3)) o "
            "WHERE t.chain_seq IN (2, 3) AND t.chain_seq <> o.chain_seq"
        )

    _assert_row_break(prov, _verify(prov), kind="row_hash_mismatch", chain_seq=2, rows=10)


@pytest.mark.parametrize("infinite", ["infinity", "-infinity"])
def test_an_infinite_timestamp_is_reported_as_a_row_hash_mismatch_naming_the_row(
    ten_rows: Provisioned, infinite: str
) -> None:
    """The trigger never chains an infinite time, so a chained row holding one was rewritten.

    The row is still named by its sequence and id. The time itself has no `datetime`, so the
    report carries none for this row.
    """
    prov = ten_rows
    event_id, _ = _row_identity(prov, 4)
    with _tampering(prov) as conn:
        conn.execute(
            "UPDATE audit_log.audit_events SET occurred_at = %s::timestamptz WHERE chain_seq = 4",
            (infinite,),
        )

    result = _verify(prov)

    assert result.intact is False
    assert result.rows_checked == 10
    assert result.first_break == AuditChainBreak(
        kind="row_hash_mismatch", chain_seq=4, event_id=event_id, occurred_at=None
    )


def test_an_infinite_timestamp_on_a_later_row_does_not_hide_an_earlier_break(
    ten_rows: Provisioned,
) -> None:
    prov = ten_rows
    with _tampering(prov) as conn:
        conn.execute("UPDATE audit_log.audit_events SET action = 'x.y' WHERE chain_seq = 3")
        conn.execute(
            "UPDATE audit_log.audit_events SET occurred_at = 'infinity' WHERE chain_seq = 8"
        )

    _assert_row_break(prov, _verify(prov), kind="row_hash_mismatch", chain_seq=3, rows=10)


def test_removed_trailing_rows_are_reported_as_a_head_mismatch(ten_rows: Provisioned) -> None:
    """Every remaining row verifies, so only the head betrays the missing tail."""
    prov = ten_rows
    with _tampering(prov) as conn:
        conn.execute("DELETE FROM audit_log.audit_events WHERE chain_seq > 8")

    result = _verify(prov)

    assert result == AuditChainVerification(
        intact=False,
        rows_checked=8,
        head_chain_seq=10,
        first_break=AuditChainBreak(
            kind="head_mismatch", chain_seq=None, event_id=None, occurred_at=None
        ),
    )


def test_a_truncated_trail_is_reported_as_a_head_mismatch(ten_rows: Provisioned) -> None:
    prov = ten_rows
    with _tampering(prov) as conn:
        conn.execute("TRUNCATE audit_log.audit_events CASCADE")

    result = _verify(prov)

    assert result.intact is False
    assert (result.rows_checked, result.head_chain_seq) == (0, 10)
    assert result.first_break is not None
    assert result.first_break.kind == "head_mismatch"


def test_a_consistent_rewrite_of_the_whole_chain_by_a_superuser_is_not_detected(
    ten_rows: Provisioned,
) -> None:
    """ACCEPTED LIMIT (out of scope per the issue): the chain is tamper-evident, not tamper-proof.

    A database superuser who changes a row and then recomputes every later hash and the head,
    with the database's own functions, leaves nothing to compare against. This test documents
    that on purpose, so nobody "fixes" it by weakening the check or by claiming more than the
    design promises. Anchoring the head outside the database is a separate piece of work.
    """
    prov = ten_rows
    with _tampering(prov) as conn:
        conn.execute(
            "UPDATE audit_log.audit_events SET details = '{\"forged\": 1}' WHERE chain_seq = 4"
        )
        conn.execute(
            """
            DO $$
            DECLARE r record; v_prev text := (SELECT row_hash FROM audit_log.audit_events
                                                WHERE chain_seq = 3);
            BEGIN
                FOR r IN SELECT * FROM audit_log.audit_events WHERE chain_seq >= 4
                         ORDER BY chain_seq LOOP
                    UPDATE audit_log.audit_events SET prev_row_hash = v_prev,
                        row_hash = audit_log.chain_hash(v_prev, audit_log.canonical_row(
                            r.chain_seq, r.id, r.occurred_at, r.actor_subject, r.actor_issuer,
                            r.action, r.resource_type, r.resource_id, r.outcome,
                            (SELECT details FROM audit_log.audit_events WHERE id = r.id)))
                        WHERE id = r.id RETURNING row_hash INTO v_prev;
                END LOOP;
                UPDATE audit_log.audit_chain_head SET last_row_hash = v_prev;
            END $$
            """
        )

    assert _verify(prov) == AuditChainVerification(
        intact=True, rows_checked=10, head_chain_seq=10, first_break=None
    )


def test_the_tool_reports_a_tampered_row_with_its_id_and_time(
    fresh_state_postgres_env: Provisioned, monkeypatch: pytest.MonkeyPatch
) -> None:
    configure()
    prov = fresh_state_postgres_env
    _write_rows(prov, _ROWS)
    with _tampering(prov) as conn:
        conn.execute("UPDATE audit_log.audit_events SET outcome = 'failed' WHERE chain_seq = 3")
    event_id, occurred_at = _row_identity(prov, 3)
    roles = FakeAccessRoleStore(expected_owner=_OWNER)
    monkeypatch.setitem(_OVERRIDES, "role_store_factory", _factory(roles))

    result = _call_tool()

    reset_for_tests()
    block = result.content[0]
    assert isinstance(block, TextContent)
    assert json.loads(block.text) == {
        "intact": False,
        "rows_checked": 10,
        "head_chain_seq": 10,
        "first_break": {
            "kind": "row_hash_mismatch",
            "chain_seq": 3,
            "id": event_id,
            "occurred_at": occurred_at.isoformat(),
        },
    }
