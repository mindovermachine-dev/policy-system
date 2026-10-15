"""`postgres_live` tests: when the chain trigger cannot run, the caller fails whole and by name.

The trigger is made to fail without any production hook. A superuser session holds `FOR UPDATE`
on the chain head, so the writer's insert blocks, and one of two timeouts ends the wait: the
application role's `statement_timeout` (SQLSTATE 57014) or the chain function's own
`lock_timeout` (55P03). A raw insert with `occurred_at = 'infinity'` covers the failure that
comes from inside the trigger. Each real caller must roll back its state change together with
the audit row and raise the error it already documents; `AuditStore.record` must let the raw
`psycopg.Error` through, since it owns no transaction.

Deselected by default -- run with `uv run pytest -m postgres_live` (see
`persistence/provisioned_postgres.py`).
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

import psycopg
import pytest
from persistence.provisioned_postgres import (
    Provisioned,
    migrate_state_database,
    provision_graph_log,
)

import ps_service.authz.audit_actions  # noqa: F401  # pyright: ignore[reportUnusedImport] -- side-effect import, registers access_role.* actions
from audit._integrity_support import (
    ISSUER,
    assert_chain_is_intact,
    chain_function_lock_timeout,
    chain_rows,
    head,
    head_row_locked,
    register_test_key,
    role_statement_timeout,
)
from ps_service.audit import AuditStore, PsycopgAuditStore
from ps_service.audit.errors import AuditPersistenceError, AuditTrailUnavailableError
from ps_service.authz.errors import AccessRoleAssignmentPersistenceError
from ps_service.authz.models import AccessRole
from ps_service.authz.store import PsycopgAccessRoleStore
from ps_service.graph_gateway.system_audit import build_system_audit_recorder
from ps_service.ingestion_runs import IngestionRunPersistenceError, PsycopgIngestionRunStore
from ps_service.persistence import connect_from_config
from ps_service.runtime_config import PsycopgRuntimeConfigStore, RuntimeConfigPersistenceError

if TYPE_CHECKING:
    from collections.abc import Iterator
    from contextlib import AbstractContextManager

pytestmark = pytest.mark.postgres_live

_ACTOR = ("failure-actor", ISSUER)
_RESULT: dict[str, object] = {"summary": "ok"}


@pytest.fixture
def prov(fresh_provisioned: Provisioned) -> Provisioned:
    """A fully provisioned cluster whose chain already holds one row (`chain_seq` 1)."""
    migrate_state_database(fresh_provisioned)
    provision_graph_log(fresh_provisioned)
    _standalone(fresh_provisioned, "seed")
    return fresh_provisioned


def _standalone(prov: Provisioned, resource_id: str) -> None:
    PsycopgAuditStore(prov.state_config()).record_standalone(
        actor_subject=_ACTOR[0],
        actor_issuer=_ACTOR[1],
        action="access_role.grant",
        resource_type="principal",
        resource_id=resource_id,
        outcome="applied",
        details={"access_role": "SystemAdmin"},
    )


@pytest.fixture(params=["statement_timeout", "lock_timeout"])
def blocked_chain(request: pytest.FixtureRequest, prov: Provisioned) -> Iterator[type[Exception]]:
    """Make every audit insert fail by name: the head is locked and a timeout ends the wait.

    Yields the exception class the blocked insert raises: `QueryCanceled` for the role's
    `statement_timeout`, `LockNotAvailable` for the chain function's own `lock_timeout`.
    """
    mode: str = request.param
    if mode == "statement_timeout":
        timeout: AbstractContextManager[None] = role_statement_timeout(prov, "500ms")
        expected: type[Exception] = psycopg.errors.QueryCanceled
    else:
        timeout = chain_function_lock_timeout(prov, "300ms")
        expected = psycopg.errors.LockNotAvailable
    with timeout, head_row_locked(prov):
        yield expected


def _assert_chain_untouched_then_continues(prov: Provisioned) -> None:
    """After the block is released: still one row, same head, and the next row is `chain_seq` 2."""
    assert [row[0] for row in chain_rows(prov)] == [1]
    first_head = head(prov)
    assert first_head[0] == 1
    _standalone(prov, "after")
    rows = chain_rows(prov)
    assert [row[0] for row in rows] == [1, 2]
    assert rows[1][1] == first_head[1]
    assert assert_chain_is_intact(prov) == 2


def test_record_standalone_raises_audit_persistence_error_and_writes_nothing(
    prov: Provisioned, blocked_chain: type[Exception]
) -> None:
    with pytest.raises(AuditPersistenceError) as raised:
        _standalone(prov, "blocked")

    assert isinstance(raised.value.__cause__, blocked_chain)


def test_record_system_audit_raises_audit_trail_unavailable(
    prov: Provisioned, blocked_chain: type[Exception]
) -> None:
    recorder = build_system_audit_recorder(prov.state_config())

    with pytest.raises(AuditTrailUnavailableError) as raised:
        recorder(action="ingestion.system", resource_type="run", resource_id="r", details={})

    assert isinstance(raised.value.__cause__, blocked_chain)


def test_record_called_directly_lets_the_raw_driver_error_through(
    prov: Provisioned, blocked_chain: type[Exception]
) -> None:
    config = prov.state_config()
    with connect_from_config(config) as conn, conn.cursor() as cur:
        with pytest.raises(psycopg.Error) as raised:
            PsycopgAuditStore(config).record(
                cur,
                actor_subject=_ACTOR[0],
                actor_issuer=_ACTOR[1],
                action="access_role.grant",
                resource_type="principal",
                resource_id="direct",
                outcome="applied",
                details={"access_role": "SystemAdmin"},
            )
        conn.rollback()

    assert type(raised.value) is blocked_chain


def test_a_runtime_config_set_rolls_back_the_state_change_with_the_audit_row(
    prov: Provisioned, blocked_chain: type[Exception]
) -> None:
    config = prov.state_config()
    key = register_test_key()
    store = PsycopgRuntimeConfigStore(config, audit_store=PsycopgAuditStore(config))

    with pytest.raises(RuntimeConfigPersistenceError) as raised:
        store.set(key, "value", actor=_ACTOR)

    assert isinstance(raised.value.__cause__, blocked_chain)
    with prov.superuser_connect(prov.state_db) as conn:
        assert conn.execute(
            "SELECT count(*) FROM runtime_config WHERE key = %s", (key,)
        ).fetchone() == (0,)


def test_an_access_role_grant_rolls_back_the_assignment_with_the_audit_row(
    prov: Provisioned, blocked_chain: type[Exception]
) -> None:
    config = prov.state_config()
    store = PsycopgAccessRoleStore(config, audit_store=PsycopgAuditStore(config))

    with pytest.raises(AccessRoleAssignmentPersistenceError) as raised:
        store.grant(
            actor=_ACTOR, target=("grant-target", ISSUER), access_role=AccessRole.SYSTEM_ADMIN
        )

    assert isinstance(raised.value.__cause__, blocked_chain)
    with prov.superuser_connect(prov.state_db) as conn:
        assert conn.execute("SELECT count(*) FROM access_role_assignments").fetchone() == (0,)


def test_an_ingestion_run_create_rolls_back_the_run_with_the_audit_row(
    prov: Provisioned, blocked_chain: type[Exception]
) -> None:
    config = prov.state_config()
    store = PsycopgIngestionRunStore(config, audit_store=PsycopgAuditStore(config))

    with pytest.raises(IngestionRunPersistenceError) as raised:
        store.create_run(
            run_id=str(uuid.uuid4()), celex="32024R2847", short_name="cra", actor=_ACTOR
        )

    assert isinstance(raised.value.__cause__, blocked_chain)
    with prov.superuser_connect(prov.state_db) as conn:
        assert conn.execute("SELECT count(*) FROM ingestion_runs").fetchone() == (0,)


def test_a_completed_run_update_rolls_back_with_its_audit_row(prov: Provisioned) -> None:
    config = prov.state_config()
    store = PsycopgIngestionRunStore(config, audit_store=PsycopgAuditStore(config))
    run_id = str(uuid.uuid4())
    store.create_run(run_id=run_id, celex="32024R2847", short_name="cra", actor=_ACTOR)

    with chain_function_lock_timeout(prov, "300ms"), head_row_locked(prov):
        with pytest.raises(IngestionRunPersistenceError) as raised:
            store.complete_run(run_id, status="succeeded", result=_RESULT, error=None)
        assert isinstance(raised.value.__cause__, psycopg.errors.LockNotAvailable)

    row = store.get_run(run_id)
    assert row is not None
    assert row.status == "running"
    assert assert_chain_is_intact(prov) == 2


def test_after_a_blocked_write_is_released_the_chain_is_unchanged_and_has_no_gap(
    prov: Provisioned,
) -> None:
    with (
        chain_function_lock_timeout(prov, "300ms"),
        head_row_locked(prov),
        pytest.raises(AuditPersistenceError),
    ):
        _standalone(prov, "blocked")

    _assert_chain_untouched_then_continues(prov)


def test_an_infinite_occurred_at_fails_inside_the_trigger_and_rolls_the_transaction_back(
    prov: Provisioned,
) -> None:
    with prov.as_state() as conn:
        conn.autocommit = False
        conn.execute(
            "INSERT INTO audit_log.audit_events "
            "(actor_subject, actor_issuer, action, resource_type, resource_id, outcome) "
            "VALUES ('a', 'i', 'raw.write', 'raw', 'same-transaction', 'applied')"
        )
        with pytest.raises(psycopg.Error) as raised:
            conn.execute(
                "INSERT INTO audit_log.audit_events "
                "(actor_subject, actor_issuer, action, resource_type, resource_id, outcome, "
                "occurred_at) "
                "VALUES ('a', 'i', 'raw.write', 'raw', 'infinite', 'applied', 'infinity')"
            )
        conn.rollback()

    assert "infinit" in str(raised.value).lower()
    _assert_chain_untouched_then_continues(prov)


def test_record_documents_the_lock_ordering_invariant() -> None:
    """Fast, no database: the invariant that keeps the head lock last is written down."""
    for owner in (PsycopgAuditStore, AuditStore):
        doc = " ".join((owner.record.__doc__ or "").split())
        assert "last statement before" in doc
        assert "chain head" in doc
