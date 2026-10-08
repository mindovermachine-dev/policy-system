"""Audit emission of the `check_regulations` / `POST /change-checks` sweep (issue #195, Slice 10).

Each re-ingest the sweep actually runs is bracketed by an `ingestion_run.submit` opening row
(fail-closed) and an `ingestion_run.complete` terminal row (best-effort) with
`trigger='amendment_check'`, the re-ingest's OWN run id as `resource_id` and the sweep caller as
actor. Hand-written fakes only (no `unittest.mock`): an in-memory audit store whose ordered
`events` list is shared with a wrapper around the fake `trigger_reingestion`.
"""

from __future__ import annotations

import dataclasses
from datetime import date
from typing import TYPE_CHECKING

import pytest

from api._audit_fakes import InMemoryAuditStore
from api._fakes import build_fake_change_check_dependencies
from ps_service.api.catalog import CatalogEntry
from ps_service.api.change_check_orchestration import (
    ChangeCheckDependencies,
    ChangeCheckResult,
    run_change_check_sweep,
)
from ps_service.audit import AuditContext, AuditPersistenceError, AuditTrailUnavailableError
from ps_service.change_monitor.errors import ChangeMonitorStateError
from ps_service.change_monitor.models import (
    AmendmentFinding,
    PollReport,
    ReingestionOutcome,
    TrackedInstrumentNode,
)

if TYPE_CHECKING:
    from api._fakes import FakeChangeCheckDependencies, MakeEmitter, ReadLines
    from ps_service.config import ServiceConfig
    from ps_service.logging import LogEmitter

_ACTOR = ("sweep-caller-sub", "https://issuer.example.com/")
_CRA = CatalogEntry(
    celex="32024R2847", title="Cyber Resilience Act", short_name="CRA", version="1.0"
)
_DORA = CatalogEntry(celex="32022R2554", title="DORA", short_name="DORA", version="1.0")


class NationalTranspositionNotSupportedError(Exception):
    """Local double matched by class name, like the production boundary does."""


class PipelineStageError(Exception):
    """Local double: classified by class name into `pipeline_stage_failed`."""


def _node(instrument_id: str, celex: str = "32024R2847") -> TrackedInstrumentNode:
    return TrackedInstrumentNode(
        regulatory_instrument_id=instrument_id,
        celex=celex,
        instrument_type="regulation",
        effective_date="2024-01-01",
    )


def _finding(instrument_id: str) -> AmendmentFinding:
    return AmendmentFinding(
        regulatory_instrument_id=instrument_id,
        instrument_type="regulation",
        baseline_reference="2024-01-01",
        detected_consolidated_celex="32024R2847C01",
        detected_consolidation_date=date(2025, 1, 1),
        reason="newer_consolidation",
    )


def _outcome(run_id: str | None = "ingest-run-1") -> ReingestionOutcome:
    return ReingestionOutcome(
        prior_regulatory_instrument_id="CRA-0.9",
        new_regulatory_instrument_id="CRA-32024R2847C01",
        run_id=run_id,
        outcome="superseded",
        ingest_counts={},
    )


def _fake(
    *,
    results: list[ReingestionOutcome | BaseException],
    tracked: tuple[TrackedInstrumentNode, ...] | None = None,
    catalog: dict[str, CatalogEntry] | None = None,
    will_reingest_error: BaseException | None = None,
) -> FakeChangeCheckDependencies:
    nodes = tracked or tuple(_node(f"CRA-{i}") for i in range(len(results)))
    return build_fake_change_check_dependencies(
        tracked=nodes,
        poll_report=PollReport(
            findings=tuple(_finding(n.regulatory_instrument_id) for n in nodes),
            polled_count=len(nodes),
            failed_ids=(),
            unconfigured_ids=(),
        ),
        catalog_entries=(
            catalog if catalog is not None else {"32024R2847": _CRA, "32022R2554": _DORA}
        ),
        reingestion_results=results,
        will_reingest_error=will_reingest_error,
    )


def _ordered(deps: ChangeCheckDependencies, events: list[str]) -> ChangeCheckDependencies:
    """Record when `trigger_reingestion` runs on the same list the audit store appends to."""
    inner = deps.trigger_reingestion

    def _trigger(*args: object, **kwargs: object) -> ReingestionOutcome:
        events.append("trigger_reingestion")
        return inner(*args, **kwargs)  # pyright: ignore[reportArgumentType]

    return dataclasses.replace(deps, trigger_reingestion=_trigger)  # pyright: ignore[reportArgumentType]


def _sweep(
    app_config: ServiceConfig,
    fake: FakeChangeCheckDependencies,
    store: InMemoryAuditStore,
    emitter: LogEmitter,
    *,
    dependencies: ChangeCheckDependencies | None = None,
) -> ChangeCheckResult:
    return run_change_check_sweep(
        config=app_config,
        run_id="sweep-run",
        dependencies=dependencies or fake.dependencies,
        audit=AuditContext(_ACTOR, store),
        emitter=emitter,
    )


def test_reingest_writes_submit_row_before_trigger_reingestion(
    app_config: ServiceConfig, make_emitter: MakeEmitter
) -> None:
    emitter, _ = make_emitter()
    store = InMemoryAuditStore()
    fake = _fake(results=[_outcome()], tracked=(_node("CRA-1"),))

    _sweep(app_config, fake, store, emitter, dependencies=_ordered(fake.dependencies, store.events))

    assert store.events == [
        "audit:ingestion_run.submit:applied",
        "trigger_reingestion",
        "audit:ingestion_run.complete:applied",
    ]


def test_reingest_pair_uses_the_reingest_run_id_as_resource_id_and_sweep_caller_as_actor(
    app_config: ServiceConfig, make_emitter: MakeEmitter
) -> None:
    emitter, _ = make_emitter()
    store = InMemoryAuditStore()
    fake = _fake(results=[_outcome()], tracked=(_node("CRA-1"),))

    result = _sweep(app_config, fake, store, emitter)

    (call,) = fake.trigger_reingestion_calls
    assert call.run_id is not None
    assert call.run_id != result.run_id  # the re-ingest's own id, not the sweep's
    assert [r.resource_id for r in store.rows] == [call.run_id, call.run_id]
    assert {(r.actor_subject, r.actor_issuer) for r in store.rows} == {_ACTOR}
    assert {r.resource_type for r in store.rows} == {"ingestion_run"}


def test_audit_resource_id_equals_the_response_reingest_run_id(
    app_config: ServiceConfig, make_emitter: MakeEmitter
) -> None:
    """The real `trigger_reingestion` returns the run id it was given, so response == audit."""
    emitter, _ = make_emitter()
    store = InMemoryAuditStore()
    fake = _fake(results=[_outcome()], tracked=(_node("CRA-1"),))
    inner = fake.dependencies.trigger_reingestion

    def _echo(*args: object, run_id: str | None = None, **kwargs: object) -> ReingestionOutcome:
        return dataclasses.replace(
            inner(*args, run_id=run_id, **kwargs),  # pyright: ignore[reportArgumentType]
            run_id=run_id,
        )

    deps = dataclasses.replace(fake.dependencies, trigger_reingestion=_echo)  # pyright: ignore[reportArgumentType]

    result = _sweep(app_config, fake, store, emitter, dependencies=deps)

    (outcome,) = result.instruments
    assert outcome.reingest_run_id == store.rows[0].resource_id == store.rows[1].resource_id


def test_reingest_submit_row_carries_base_celex_short_name_and_trigger_amendment_check(
    app_config: ServiceConfig, make_emitter: MakeEmitter
) -> None:
    emitter, _ = make_emitter()
    store = InMemoryAuditStore()

    _sweep(app_config, _fake(results=[_outcome()], tracked=(_node("CRA-1"),)), store, emitter)

    assert (store.rows[0].action, store.rows[0].outcome) == ("ingestion_run.submit", "applied")
    assert store.rows[0].details == {
        "celex": "32024R2847",
        "short_name": "CRA",
        "status": "started",
        "trigger": "amendment_check",
    }


def test_reingest_complete_row_carries_new_instrument_id_and_zero_counts(
    app_config: ServiceConfig, make_emitter: MakeEmitter
) -> None:
    emitter, _ = make_emitter()
    store = InMemoryAuditStore()

    _sweep(app_config, _fake(results=[_outcome()], tracked=(_node("CRA-1"),)), store, emitter)

    complete = store.rows[1]
    assert (complete.action, complete.outcome) == ("ingestion_run.complete", "applied")
    assert complete.details == {
        "status": "succeeded",
        "celex": "32024R2847",
        "trigger": "amendment_check",
        "regulatory_instrument_id": "CRA-32024R2847C01",
        "outcome": "fresh",
        "new_obligations": 0,
        "new_capabilities": 0,
        "matched_capabilities": 0,
    }


@pytest.mark.parametrize("run_id", [None])
def test_already_processed_and_resumed_reingests_write_no_audit_row(
    run_id: str | None, app_config: ServiceConfig, make_emitter: MakeEmitter
) -> None:
    """`resume` / `already_processed` ingest nothing (CHANGES F-4): no pair, outcome unchanged."""
    emitter, _ = make_emitter()
    store = InMemoryAuditStore()
    fake = _fake(results=[_outcome(run_id)], tracked=(_node("CRA-1"),))

    result = _sweep(app_config, fake, store, emitter)

    assert store.rows == []
    assert [o.outcome for o in result.instruments] == ["amendment_reingested"]
    assert len(fake.trigger_reingestion_calls) == 1
    assert fake.trigger_reingestion_calls[0].run_id is None


def test_national_transposition_skip_records_failed_unsupported_instrument_type(
    app_config: ServiceConfig, make_emitter: MakeEmitter
) -> None:
    emitter, _ = make_emitter()
    store = InMemoryAuditStore()
    fake = _fake(
        results=[NationalTranspositionNotSupportedError("guard text")], tracked=(_node("CRA-1"),)
    )

    result = _sweep(app_config, fake, store, emitter)

    assert [o.outcome for o in result.instruments] == ["skipped"]
    complete = store.rows[1]
    assert complete.outcome == "failed"
    assert complete.details["status"] == "failed"
    assert complete.details["reason_code"] == "unsupported_instrument_type"
    assert "guard text" not in str(store.rows)


def test_reingest_failure_records_failed_with_classified_reason_code_and_sweep_continues(
    app_config: ServiceConfig, make_emitter: MakeEmitter
) -> None:
    emitter, _ = make_emitter()
    store = InMemoryAuditStore()
    fake = _fake(
        results=[PipelineStageError("boom /srv/secret/path"), _outcome()],
        tracked=(_node("CRA-1"), _node("CRA-2")),
    )

    result = _sweep(app_config, fake, store, emitter)

    assert [o.outcome for o in result.instruments] == ["reingest_failed", "amendment_reingested"]
    failed = store.rows[1]
    assert failed.outcome == "failed"
    assert failed.details["reason_code"] == "pipeline_stage_failed"
    assert "boom" not in str(store.rows) and "/srv" not in str(store.rows)
    assert [r.outcome for r in store.rows] == ["applied", "failed", "applied", "applied"]


def test_two_amended_instruments_record_two_distinct_pairs(
    app_config: ServiceConfig, make_emitter: MakeEmitter
) -> None:
    emitter, _ = make_emitter()
    store = InMemoryAuditStore()
    fake = _fake(
        results=[_outcome("a"), _outcome("b")],
        tracked=(_node("CRA-1"), _node("DORA-1", "32022R2554")),
    )

    _sweep(app_config, fake, store, emitter)

    ids = [r.resource_id for r in store.rows]
    assert ids[0] == ids[1] and ids[2] == ids[3] and ids[0] != ids[2]
    assert [r.details["celex"] for r in store.rows[::2]] == ["32024R2847", "32022R2554"]


def test_instrument_without_catalog_entry_writes_no_row(
    app_config: ServiceConfig, make_emitter: MakeEmitter
) -> None:
    emitter, _ = make_emitter()
    store = InMemoryAuditStore()
    fake = _fake(results=[], tracked=(_node("CRA-1"),), catalog={})

    result = _sweep(app_config, fake, store, emitter)

    assert [o.outcome for o in result.instruments] == ["reingest_failed"]
    assert store.rows == []


def test_probe_failure_is_isolated_and_writes_no_row(
    app_config: ServiceConfig, make_emitter: MakeEmitter
) -> None:
    emitter, _ = make_emitter()
    store = InMemoryAuditStore()
    fake = _fake(
        results=[], tracked=(_node("CRA-1"),), will_reingest_error=ChangeMonitorStateError("x")
    )

    result = _sweep(app_config, fake, store, emitter)

    assert [o.outcome for o in result.instruments] == ["reingest_failed"]
    assert store.rows == []
    assert fake.trigger_reingestion_calls == []


def test_sweep_aborts_with_audit_trail_unavailable_when_a_submit_row_cannot_be_written(
    app_config: ServiceConfig, make_emitter: MakeEmitter
) -> None:
    emitter, _ = make_emitter()
    store = InMemoryAuditStore(fail_on_outcome={"applied": AuditPersistenceError("db down")})
    fake = _fake(results=[_outcome()], tracked=(_node("CRA-1"),))

    with pytest.raises(AuditTrailUnavailableError):
        _sweep(app_config, fake, store, emitter)

    assert fake.trigger_reingestion_calls == []


def test_sweep_abort_keeps_the_pairs_of_earlier_instruments_and_emits_a_failed_sweep_entry(
    app_config: ServiceConfig, make_emitter: MakeEmitter, read_lines: ReadLines
) -> None:
    emitter, log_path = make_emitter()
    store = InMemoryAuditStore()
    fake = _fake(results=[_outcome("a"), _outcome("b")], tracked=(_node("CRA-1"), _node("CRA-2")))
    inner_record = store.record_standalone
    submits: list[str] = []

    def _second_submit_fails(**kwargs: object) -> None:
        if kwargs["action"] == "ingestion_run.submit":
            submits.append(str(kwargs["resource_id"]))
            if len(submits) == 2:
                raise AuditPersistenceError("db down")
        inner_record(**kwargs)  # pyright: ignore[reportArgumentType]

    store.record_standalone = _second_submit_fails  # type: ignore[method-assign]  # pyright: ignore[reportAttributeAccessIssue]

    with pytest.raises(AuditTrailUnavailableError):
        _sweep(app_config, fake, store, emitter)
    emitter.flush()

    assert [r.action for r in store.rows] == ["ingestion_run.submit", "ingestion_run.complete"]
    assert len(fake.trigger_reingestion_calls) == 1
    sweep_entries = [
        line
        for line in read_lines(log_path)
        if line["action"] == "change_check_sweep" and line["outcome"] == "failed"
    ]
    assert len(sweep_entries) == 1
    assert sweep_entries[0]["run_id"] == "sweep-run"


def test_terminal_row_failure_is_logged_with_reingest_run_id_and_sweep_result_unchanged(
    app_config: ServiceConfig, make_emitter: MakeEmitter, read_lines: ReadLines
) -> None:
    emitter, log_path = make_emitter()
    store = InMemoryAuditStore(fail_on_outcome={"failed": AuditPersistenceError("db down")})
    fake = _fake(results=[PipelineStageError("x")], tracked=(_node("CRA-1"),))

    result = _sweep(app_config, fake, store, emitter)
    emitter.flush()

    assert [o.outcome for o in result.instruments] == ["reingest_failed"]
    run_id = fake.trigger_reingestion_calls[0].run_id
    entries = [e for e in read_lines(log_path) if e["action"] == "audit_terminal_failed"]
    assert [e["run_id"] for e in entries] == [run_id]
