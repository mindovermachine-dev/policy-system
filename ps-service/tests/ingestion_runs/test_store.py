"""Tests for the `IngestionRunStore` contract and `PsycopgIngestionRunStore` (issue #194).

The contract tests run against the in-memory fake (fast) and, `postgres_live`-marked, the
real store on an isolated schema with the migrations applied. The fast Psycopg tests prove
what is decided without a database: the fixed no-detail unavailable error and that an
unparseable run id never reaches the database.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, cast

import psycopg
import pytest

from ingestion_runs._fakes import InMemoryIngestionRunStore, RecordingAuditStore
from ps_service.audit import AuditQueryFilters, PsycopgAuditStore
from ps_service.config import ServiceConfig, load_config
from ps_service.ingestion_runs import (
    IngestionRunInvalidCompletionError,
    IngestionRunPersistenceError,
    IngestionRunStoreUnavailableError,
    PsycopgIngestionRunStore,
)
from ps_service.persistence import apply_pending_migrations, connect_from_config
from ps_service.state_migrations import ORDINARY_STATE_MIGRATION_SOURCES

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping
    from typing import Literal, LiteralString

    from psycopg.rows import TupleRow

    from ps_service.ingestion_runs import IngestionRunStore

_ACTOR = ("actor-subject", "https://issuer.example.com/")
_UNAVAILABLE = "The ingestion run store is temporarily unavailable."
_RESULT: dict[str, object] = {
    "run_id": "r",
    "regulatory_instrument_id": "cra-1.0",
    "source": "catalog",
    "outcome": "fresh",
    "stages": [{"stage": "ingestion", "status": "succeeded", "summary": 3}],
}


def _config(*, host: str | None, port: int = 5432) -> ServiceConfig:
    return ServiceConfig(
        host="127.0.0.1",
        port=8000,
        graceful_shutdown_seconds=10,
        logging_dir=None,
        state_postgres_host=host,
        state_postgres_port=port,
        state_postgres_database="ps_state",
        state_postgres_user="ps_state",
        state_postgres_password="unused",
    )


@pytest.fixture(name="live_config")
def _live_config_on_isolated_schema(  # pyright: ignore[reportUnusedFunction]  # pytest fixture used by name in the live tests and the store fixture
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[ServiceConfig]:
    """The real config, with every connection pinned to a throwaway schema with migrations applied.

    libpq's `PGOPTIONS` pins every connection opened while it is set (the migration runner's and
    the stores' own) to the isolated schema, so no production code needs a test-only seam.
    """
    config = load_config()
    assert config.state_postgres_host is not None, (
        "postgres_live requires PS_STATE_POSTGRES_HOST to be set"
    )
    schema = f"ingestion_runs_test_{uuid.uuid4().hex}"
    with connect_from_config(config) as conn:
        conn.execute(cast("LiteralString", f'CREATE SCHEMA "{schema}"'))
    monkeypatch.setenv("PGOPTIONS", f"-c search_path={schema}")
    with connect_from_config(config) as conn:
        apply_pending_migrations(conn, sources=ORDINARY_STATE_MIGRATION_SOURCES)
    try:
        yield config
    finally:
        monkeypatch.delenv("PGOPTIONS")
        with connect_from_config(config) as conn:
            conn.execute(cast("LiteralString", f'DROP SCHEMA "{schema}" CASCADE'))


@pytest.fixture
def _psycopg_store_on_isolated_schema(  # pyright: ignore[reportUnusedFunction]  # pytest fixture referenced by name through `request.getfixturevalue`
    live_config: ServiceConfig,
) -> PsycopgIngestionRunStore:
    config = live_config
    return PsycopgIngestionRunStore(config, audit_store=PsycopgAuditStore(config))


def _memory() -> IngestionRunStore:
    return InMemoryIngestionRunStore()


_FAST = pytest.param(_memory, id="memory")
_LIVE = pytest.param(
    "_psycopg_store_on_isolated_schema", id="psycopg", marks=pytest.mark.postgres_live
)


@pytest.fixture(params=[_FAST, _LIVE])
def store(request: pytest.FixtureRequest) -> IngestionRunStore:
    """The store under contract test: the in-memory fake, or the real one when live."""
    param = cast("Callable[[], IngestionRunStore] | str", request.param)
    if isinstance(param, str):
        return cast("IngestionRunStore", request.getfixturevalue(param))
    return param()


def _new_id() -> str:
    return str(uuid.uuid4())


def test_create_run_returns_a_running_row_with_no_result(store: IngestionRunStore) -> None:
    run_id = _new_id()

    row = store.create_run(run_id=run_id, celex="32024R2847", short_name="cra", actor=_ACTOR)

    assert row.run_id == run_id
    assert (row.celex, row.short_name) == ("32024R2847", "cra")
    assert (row.actor_subject, row.actor_issuer) == _ACTOR
    assert row.status == "running"
    assert row.result is None
    assert row.error is None
    assert row.finished_at is None


def test_get_run_round_trips_a_created_row(store: IngestionRunStore) -> None:
    run_id = _new_id()
    created = store.create_run(run_id=run_id, celex="32024R2847", short_name="cra", actor=_ACTOR)

    assert store.get_run(run_id) == created


def test_get_run_of_an_unknown_uuid_returns_none(store: IngestionRunStore) -> None:
    assert store.get_run(_new_id()) is None


def test_get_run_of_a_non_uuid_returns_none(store: IngestionRunStore) -> None:
    assert store.get_run("not-a-uuid") is None


def test_complete_run_wins_once_and_keeps_the_first_result(store: IngestionRunStore) -> None:
    """AC-BI-008: the terminal write is a single-winner compare-and-swap."""
    run_id = _new_id()
    store.create_run(run_id=run_id, celex="32024R2847", short_name="cra", actor=_ACTOR)

    first = store.complete_run(run_id, status="succeeded", result=_RESULT, error=None)
    second = store.complete_run(
        run_id, status="failed", result=None, error="error: late", reason_code="interrupted"
    )

    assert first is True
    assert second is False
    row = store.get_run(run_id)
    assert row is not None
    assert row.status == "succeeded"
    assert row.result == _RESULT
    assert row.error is None
    assert row.finished_at is not None


def test_complete_run_failed_stores_the_error_text(store: IngestionRunStore) -> None:
    run_id = _new_id()
    store.create_run(run_id=run_id, celex="32024R2847", short_name="cra", actor=_ACTOR)

    completed = store.complete_run(
        run_id, status="failed", result=None, error="error: boom", reason_code="unexpected_error"
    )

    assert completed is True
    row = store.get_run(run_id)
    assert row is not None
    assert (row.status, row.result, row.error) == ("failed", None, "error: boom")


def test_complete_run_rejects_a_failure_without_a_reason_code_and_leaves_the_run_running(
    store: IngestionRunStore,
) -> None:
    run_id = _new_id()
    store.create_run(run_id=run_id, celex="32024R2847", short_name="cra", actor=_ACTOR)

    with pytest.raises(IngestionRunInvalidCompletionError):
        store.complete_run(run_id, status="failed", result=None, error="error: boom")

    row = store.get_run(run_id)
    assert row is not None
    assert (row.status, row.error) == ("running", None)


def test_complete_run_rejects_a_success_carrying_a_reason_code(store: IngestionRunStore) -> None:
    run_id = _new_id()
    store.create_run(run_id=run_id, celex="32024R2847", short_name="cra", actor=_ACTOR)

    with pytest.raises(IngestionRunInvalidCompletionError):
        store.complete_run(
            run_id, status="succeeded", result=_RESULT, error=None, reason_code="interrupted"
        )

    row = store.get_run(run_id)
    assert row is not None
    assert row.status == "running"


def test_unconfigured_psycopg_store_raises_the_fixed_unavailable_message() -> None:
    store = PsycopgIngestionRunStore(_config(host=None), audit_store=RecordingAuditStore())

    with pytest.raises(IngestionRunStoreUnavailableError) as exc_info:
        store.get_run(_new_id())

    assert str(exc_info.value) == _UNAVAILABLE


def test_unreachable_psycopg_store_raises_the_same_message_without_host_or_port() -> None:
    store = PsycopgIngestionRunStore(
        _config(host="127.0.0.1", port=1), audit_store=RecordingAuditStore()
    )

    with pytest.raises(IngestionRunStoreUnavailableError) as exc_info:
        store.get_run(_new_id())

    message = str(exc_info.value)
    assert message == _UNAVAILABLE
    assert "127.0.0.1" not in message
    assert "psycopg" not in message.lower()
    assert isinstance(exc_info.value.__cause__, psycopg.Error)


def test_unreachable_psycopg_store_create_raises_the_unavailable_message() -> None:
    store = PsycopgIngestionRunStore(
        _config(host="127.0.0.1", port=1), audit_store=RecordingAuditStore()
    )

    with pytest.raises(IngestionRunStoreUnavailableError) as exc_info:
        store.create_run(run_id=_new_id(), celex="32024R2847", short_name="cra", actor=_ACTOR)

    assert str(exc_info.value) == _UNAVAILABLE


def test_get_run_of_a_non_uuid_never_attempts_a_connection() -> None:
    # An unconfigured store raises on any connect attempt, so a clean `None` proves none was made.
    assert (
        PsycopgIngestionRunStore(_config(host=None), audit_store=RecordingAuditStore()).get_run(
            "not-a-uuid"
        )
        is None
    )


# --- S7: durable audit trail (AC-BI-016 / AC-BI-017), real Postgres + real `PsycopgAuditStore` ---

_SUBMIT_ACTION = "ingestion_run.submit"
_COMPLETE_ACTION = "ingestion_run.complete"
_RECONCILER = ("system:ingestion-run-reconciler", "system:ingestion-run-reconciler")


def _audit_rows(
    config: ServiceConfig, run_id: str
) -> list[tuple[str, str, str, str, str, dict[str, object]]]:
    """`(actor_subject, actor_issuer, action, resource_type, outcome, details)` per audit row."""
    with connect_from_config(config) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT actor_subject, actor_issuer, action, resource_type, outcome, details "
            "FROM audit_events WHERE resource_id = %(run_id)s ORDER BY occurred_at, id",
            {"run_id": run_id},
        )
        return [
            (subject, issuer, action, resource_type, outcome, details)
            for subject, issuer, action, resource_type, outcome, details in cur.fetchall()
        ]


def _live_store(config: ServiceConfig) -> PsycopgIngestionRunStore:
    return PsycopgIngestionRunStore(config, audit_store=PsycopgAuditStore(config))


class _RaisingAuditStore(RecordingAuditStore):
    """`AuditStore` whose `record` fails like a broken `audit_events` insert would."""

    def record(
        self,
        cur: psycopg.Cursor[TupleRow],
        *,
        actor_subject: str,
        actor_issuer: str,
        action: str,
        resource_type: str,
        resource_id: str,
        outcome: Literal["applied", "rejected", "failed"],
        details: Mapping[str, object],
    ) -> str:
        del cur, actor_subject, actor_issuer, action, resource_type, resource_id, outcome, details
        raise psycopg.errors.OperationalError("simulated audit_events insert failure")


@pytest.mark.postgres_live
def test_create_run_writes_one_submit_audit_row_in_the_same_transaction(
    live_config: ServiceConfig,
) -> None:
    """AC-BI-016: one `applied` row, `details.status='started'`, under the submitting actor."""
    config = live_config
    run_id = _new_id()

    _live_store(config).create_run(
        run_id=run_id, celex="32024R2847", short_name="cra", actor=_ACTOR
    )

    assert _audit_rows(config, run_id) == [
        (
            *_ACTOR,
            _SUBMIT_ACTION,
            "ingestion_run",
            "applied",
            {
                "celex": "32024R2847",
                "short_name": "cra",
                "status": "started",
                "trigger": "async_ingest",
            },
        )
    ]


@pytest.mark.postgres_live
def test_each_run_has_exactly_one_submit_and_one_complete_row(
    live_config: ServiceConfig,
) -> None:
    """AC-BI-014: a lost-race second completion and re-reads add no rows."""
    config = live_config
    store = _live_store(config)
    run_id = _new_id()
    store.create_run(run_id=run_id, celex="32024R2847", short_name="cra", actor=_ACTOR)
    store.complete_run(run_id, status="succeeded", result=_RESULT, error=None)
    store.complete_run(run_id, status="failed", result=None, error="x", reason_code="interrupted")
    store.get_run(run_id)

    assert [row[2] for row in _audit_rows(config, run_id)] == [_SUBMIT_ACTION, _COMPLETE_ACTION]


@pytest.mark.postgres_live
def test_complete_run_writes_one_complete_audit_row_and_a_lost_race_writes_none(
    live_config: ServiceConfig,
) -> None:
    """AC-BI-017: the CAS winner audits under the submitter; a second completion adds no row."""
    config = live_config
    store = _live_store(config)
    run_id = _new_id()
    store.create_run(run_id=run_id, celex="32024R2847", short_name="cra", actor=_ACTOR)

    assert store.complete_run(run_id, status="succeeded", result=_RESULT, error=None) is True
    assert (
        store.complete_run(
            run_id, status="failed", result=None, error="error: late", reason_code="interrupted"
        )
        is False
    )

    rows = _audit_rows(config, run_id)
    assert [row[2] for row in rows] == [_SUBMIT_ACTION, _COMPLETE_ACTION]
    assert rows[1] == (
        *_ACTOR,
        _COMPLETE_ACTION,
        "ingestion_run",
        "applied",
        {
            "status": "succeeded",
            "celex": "32024R2847",
            "trigger": "async_ingest",
            "regulatory_instrument_id": "cra-1.0",
            "outcome": "fresh",
            "new_obligations": 0,
            "new_capabilities": 0,
            "matched_capabilities": 0,
        },
    )


@pytest.mark.postgres_live
def test_a_failed_completion_is_audited_as_failed_with_a_reason_code_and_no_error_text(
    live_config: ServiceConfig,
) -> None:
    config = live_config
    store = _live_store(config)
    run_id = _new_id()
    store.create_run(run_id=run_id, celex="32024R2847", short_name="cra", actor=_ACTOR)

    store.complete_run(
        run_id,
        status="failed",
        result=None,
        error="error: boom",
        reason_code="pipeline_stage_failed",
    )

    complete = _audit_rows(config, run_id)[1]
    assert complete[2:5] == (_COMPLETE_ACTION, "ingestion_run", "failed")
    assert complete[5] == {
        "status": "failed",
        "celex": "32024R2847",
        "trigger": "async_ingest",
        "reason_code": "pipeline_stage_failed",
        "new_obligations": 0,
        "new_capabilities": 0,
        "matched_capabilities": 0,
    }


@pytest.mark.postgres_live
def test_an_audit_actor_override_is_the_actor_recorded_on_the_completion_row(
    live_config: ServiceConfig,
) -> None:
    """OQ-7 / A6: the reconciler's completion is attributed to the sentinel, not the submitter."""
    config = live_config
    store = _live_store(config)
    run_id = _new_id()
    store.create_run(run_id=run_id, celex="32024R2847", short_name="cra", actor=_ACTOR)

    store.complete_run(
        run_id,
        status="failed",
        result=None,
        error="error: interrupted",
        reason_code="interrupted",
        audit_actor=_RECONCILER,
    )

    complete = _audit_rows(config, run_id)[1]
    assert complete[:2] == _RECONCILER
    assert (_audit_rows(config, run_id)[0][0], _audit_rows(config, run_id)[0][1]) == _ACTOR
    assert complete[5]["celex"] == "32024R2847"  # read from the run row (RETURNING celex)
    assert complete[5]["reason_code"] == "interrupted"
    row = store.get_run(run_id)
    assert row is not None
    assert (row.actor_subject, row.actor_issuer) == _ACTOR


@pytest.mark.postgres_live
def test_a_failing_submit_audit_write_rolls_back_the_run_row(
    live_config: ServiceConfig,
) -> None:
    """Atomicity: no `ingestion_runs` row without its audit row."""
    config = live_config
    run_id = _new_id()
    store = PsycopgIngestionRunStore(config, audit_store=_RaisingAuditStore())

    with pytest.raises(IngestionRunPersistenceError):
        store.create_run(run_id=run_id, celex="32024R2847", short_name="cra", actor=_ACTOR)

    assert _live_store(config).get_run(run_id) is None
    assert _audit_rows(config, run_id) == []


@pytest.mark.postgres_live
def test_a_failing_complete_audit_write_leaves_the_run_running(
    live_config: ServiceConfig,
) -> None:
    """Atomicity: the CAS and its audit row commit together or not at all."""
    config = live_config
    run_id = _new_id()
    _live_store(config).create_run(
        run_id=run_id, celex="32024R2847", short_name="cra", actor=_ACTOR
    )
    broken = PsycopgIngestionRunStore(config, audit_store=_RaisingAuditStore())

    with pytest.raises(IngestionRunPersistenceError):
        broken.complete_run(run_id, status="succeeded", result=_RESULT, error=None)

    row = _live_store(config).get_run(run_id)
    assert row is not None
    assert row.status == "running"
    assert [r[2] for r in _audit_rows(config, run_id)] == [_SUBMIT_ACTION]


@pytest.mark.postgres_live
def test_the_audit_rows_are_readable_through_the_list_audit_events_query_path(
    live_config: ServiceConfig,
) -> None:
    config = live_config
    store = _live_store(config)
    run_id = _new_id()
    store.create_run(run_id=run_id, celex="32024R2847", short_name="cra", actor=_ACTOR)
    store.complete_run(run_id, status="succeeded", result=_RESULT, error=None)

    page = PsycopgAuditStore(config).query(
        filters=AuditQueryFilters(resource_type="ingestion_run", resource_id=run_id),
        cursor=None,
        page_size=10,
    )

    assert sorted(event.action for event in page.events) == [_COMPLETE_ACTION, _SUBMIT_ACTION]
    assert {event.actor_subject for event in page.events} == {_ACTOR[0]}
