"""Audit emission of the synchronous ingestion orchestration (issue #195, Slices 8 and 9).

`run_audited_catalog_ingestion` wraps identity resolution and the pipeline between an
`ingestion_run.submit` opening row (`trigger='sync_ingest'`, fail-closed) and one
`ingestion_run.complete` terminal row (best-effort). Hand-written structural fakes (no
`unittest.mock`): a recording `InMemoryAuditStore` whose ordered `events` list is shared with a
wrapper around the fake pipeline stages, so audit-before-effect is asserted on real ordering.
"""

from __future__ import annotations

import dataclasses
import re
from typing import TYPE_CHECKING

import pytest

from api._audit_fakes import InMemoryAuditStore
from api._fakes import build_fake_pipeline_dependencies
from ps_service.api.errors import (
    CatalogIdentifierNotFoundError,
    CelexAlreadyIngestedError,
    IngestionConfigIncompleteError,
    PipelineStageError,
    ShortNameCollisionError,
)
from ps_service.api.ingestion_orchestration import (
    GraphOpeners,
    PipelineDependencies,
    run_audited_catalog_ingestion,
)
from ps_service.audit import (
    AuditContext,
    AuditPersistenceError,
    AuditPostgresUnavailableError,
    AuditTrailUnavailableError,
)
from ps_service.config import ServiceConfig
from ps_service.domain_mapper.errors import DomainMapperExtractionError
from ps_service.ingestion.adapters.errors import CellarNotFoundError

if TYPE_CHECKING:
    from pathlib import Path

    from company_merge._fakes import MakeEmitter, ReadLines

    from ps_service.company_merge.models import MergeResult
    from ps_service.ingestion.models import IngestResult
    from ps_service.logging import LogEmitter

_ACTOR = ("actor-sub", "https://issuer.example.com/")
_CELEX = "32024R2847"
_RUN_ID = "11111111-1111-4111-8111-111111111111"


@pytest.fixture(autouse=True)
def _configure_logging(configured_logging: Path) -> None:  # pyright: ignore[reportUnusedFunction]  # autouse
    """The pipeline and the identity check always log: install a real Logging facade."""


def _config() -> ServiceConfig:
    return ServiceConfig(
        host="127.0.0.1",
        port=8000,
        graceful_shutdown_seconds=10,
        logging_dir=None,
        llm_interface_model="azure/gpt-4o",
        llm_interface_embed_model="azure/text-embedding-3-large",
        company_merge_similarity_threshold=0.83,
    )


def _ordered(deps: PipelineDependencies, events: list[str]) -> PipelineDependencies:
    """Wrap the ingest stage and the merge stage so `events` records when each one runs.

    The merge wrapper also reports non-zero net-new / matched counts (the fake stage reports 0).
    """
    inner_ingest = deps.stages.ingest
    inner_merge = deps.stages.merge

    def _ingest(*args: object, **kwargs: object) -> IngestResult:
        events.append("stage:ingest")
        return inner_ingest(*args, **kwargs)  # pyright: ignore[reportArgumentType]

    def _merge(*args: object, **kwargs: object) -> MergeResult:
        result = inner_merge(*args, **kwargs)  # pyright: ignore[reportArgumentType]
        return dataclasses.replace(
            result, new_obligation_count=7, new_capability_count=2, matched_capability_count=3
        )

    return dataclasses.replace(
        deps,
        stages=dataclasses.replace(deps.stages, ingest=_ingest, merge=_merge),  # pyright: ignore[reportArgumentType]
    )


def _run(
    store: InMemoryAuditStore,
    deps: PipelineDependencies,
    *,
    config: ServiceConfig | None = None,
    emitter: LogEmitter | None = None,
) -> object:
    return run_audited_catalog_ingestion(
        _CELEX,
        "cra",
        config=config or _config(),
        run_id=_RUN_ID,
        caller="10.1.2.3",
        dependencies=deps,
        audit=AuditContext(_ACTOR, store),
        emitter=emitter,
    )


def test_audited_ingestion_writes_submit_row_before_the_first_stage() -> None:
    store = InMemoryAuditStore()
    deps = _ordered(build_fake_pipeline_dependencies().dependencies, store.events)

    _run(store, deps)

    assert store.events[:2] == ["audit:ingestion_run.submit:applied", "stage:ingest"]
    submit = store.rows[0]
    assert (submit.action, submit.outcome) == ("ingestion_run.submit", "applied")
    assert submit.details == {
        "celex": _CELEX,
        "short_name": "CRA",
        "status": "started",
        "trigger": "sync_ingest",
    }
    assert (submit.actor_subject, submit.actor_issuer) == _ACTOR


def test_audited_ingestion_writes_complete_row_with_instrument_id_and_counts() -> None:
    store = InMemoryAuditStore()
    deps = _ordered(build_fake_pipeline_dependencies(rid="CRA-1.0").dependencies, store.events)

    _run(store, deps)

    assert [row.action for row in store.rows] == ["ingestion_run.submit", "ingestion_run.complete"]
    complete = store.rows[1]
    assert complete.outcome == "applied"
    assert complete.details == {
        "status": "succeeded",
        "celex": _CELEX,
        "trigger": "sync_ingest",
        "regulatory_instrument_id": "CRA-1.0",
        "outcome": "fresh",
        "new_obligations": 7,
        "new_capabilities": 2,
        "matched_capabilities": 3,
    }
    assert (complete.actor_subject, complete.actor_issuer) == _ACTOR


def test_audited_ingestion_submit_and_complete_share_the_run_id_as_resource_id() -> None:
    store = InMemoryAuditStore()

    _run(store, build_fake_pipeline_dependencies().dependencies)

    assert [(row.resource_type, row.resource_id) for row in store.rows] == [
        ("ingestion_run", _RUN_ID),
        ("ingestion_run", _RUN_ID),
    ]


def test_audited_ingestion_returns_the_pipeline_outcome_unchanged() -> None:
    store = InMemoryAuditStore()

    outcome = _run(store, build_fake_pipeline_dependencies(rid="CRA-1.0").dependencies)

    assert getattr(outcome, "regulatory_instrument_id", None) == "CRA-1.0"
    assert getattr(outcome, "outcome", None) == "fresh"


# --- Slice 9: edge cases ------------------------------------------------------------------------

_ZERO_COUNTS = {"new_obligations": 0, "new_capabilities": 0, "matched_capabilities": 0}


def _terminal(store: InMemoryAuditStore) -> dict[str, object]:
    assert [row.action for row in store.rows] == ["ingestion_run.submit", "ingestion_run.complete"]
    return store.rows[1].details


def test_celex_already_ingested_writes_already_ingested_zero_counts_row_and_still_raises() -> None:
    """D-A / AC-BI-006: the submit row precedes the identity check; the error is unchanged."""
    store = InMemoryAuditStore()
    deps = build_fake_pipeline_dependencies(celex_row="cra-1.0").dependencies

    with pytest.raises(CelexAlreadyIngestedError):
        _run(store, deps)

    assert store.rows[1].outcome == "applied"
    assert _terminal(store) == {
        "status": "succeeded",
        "celex": _CELEX,
        "trigger": "sync_ingest",
        "outcome": "already_ingested",
        **_ZERO_COUNTS,
    }


def test_pipeline_preflight_already_ingested_writes_complete_row_already_ingested_zero_counts() -> (
    None
):
    store = InMemoryAuditStore()
    fake = build_fake_pipeline_dependencies(rid="CRA-1.0", preflight_hit=True)

    outcome = _run(store, fake.dependencies)

    assert getattr(outcome, "outcome", None) == "already_ingested"
    assert _terminal(store) == {
        "status": "succeeded",
        "celex": _CELEX,
        "trigger": "sync_ingest",
        "regulatory_instrument_id": "CRA-1.0",
        "outcome": "already_ingested",
        **_ZERO_COUNTS,
    }
    assert fake.recorder.calls == []


def _failed_terminal(store: InMemoryAuditStore, reason: str) -> None:
    assert store.rows[1].outcome == "failed"
    assert _terminal(store) == {
        "status": "failed",
        "celex": _CELEX,
        "trigger": "sync_ingest",
        "reason_code": reason,
        **_ZERO_COUNTS,
    }


def test_short_name_collision_writes_failed_row_reason_short_name_collision() -> None:
    store = InMemoryAuditStore()
    deps = build_fake_pipeline_dependencies(collision_row=("CRA-1.0", "32000R0001")).dependencies

    with pytest.raises(ShortNameCollisionError):
        _run(store, deps)

    _failed_terminal(store, "short_name_collision")


def test_unknown_celex_writes_failed_row_reason_celex_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _not_found(celex: str) -> bytes:
        raise CellarNotFoundError(celex)

    monkeypatch.setattr("ps_service.api.ingestion_orchestration.fetch_xhtml", _not_found)
    store = InMemoryAuditStore()

    with pytest.raises(CatalogIdentifierNotFoundError):
        _run(store, build_fake_pipeline_dependencies().dependencies)

    _failed_terminal(store, "celex_not_found")


def test_stage_failure_writes_failed_row_pipeline_stage_failed_without_stage_reason_text() -> None:
    store = InMemoryAuditStore()
    secret = "unit 7 broke at /Users/dev/secret/path.py host=10.0.0.1:6379"
    deps = build_fake_pipeline_dependencies(
        extract_error=DomainMapperExtractionError(secret)
    ).dependencies

    with pytest.raises(PipelineStageError) as raised:
        _run(store, deps)

    assert "unit 7" in raised.value.reason  # the caller still gets the (scrubbed) stage reason
    _failed_terminal(store, "pipeline_stage_failed")
    dumped = repr(store.rows)
    assert "unit 7" not in dumped
    assert "extraction" not in dumped


def test_incomplete_config_writes_failed_row_reason_config_incomplete() -> None:
    store = InMemoryAuditStore()
    bare = ServiceConfig(
        host="127.0.0.1", port=8000, graceful_shutdown_seconds=10, logging_dir=None
    )

    with pytest.raises(IngestionConfigIncompleteError):
        _run(store, build_fake_pipeline_dependencies().dependencies, config=bare)

    _failed_terminal(store, "config_incomplete")


def test_unexpected_exception_writes_failed_row_unexpected_error_with_no_traceback_or_path() -> (
    None
):
    store = InMemoryAuditStore()
    deps = build_fake_pipeline_dependencies().dependencies

    def _boom(_config: object) -> object:
        message = "boom /Users/dev/x.py 10.0.0.1:6379"
        raise RuntimeError(message)

    broken = dataclasses.replace(
        deps,
        graphs=GraphOpeners(
            native=deps.graphs.native,
            baseline=deps.graphs.baseline,
            single_tenant=_boom,  # pyright: ignore[reportArgumentType]
        ),
    )

    with pytest.raises(RuntimeError):
        _run(store, broken)

    _failed_terminal(store, "unexpected_error")


def test_pipeline_does_not_run_when_the_submit_row_cannot_be_written() -> None:
    """AC-BI-011: fail-closed; zero stage calls and no graph opened."""
    store = InMemoryAuditStore(
        fail_on_outcome={"applied": AuditPostgresUnavailableError("db down")}
    )
    fake = build_fake_pipeline_dependencies()
    opened: list[str] = []
    deps = dataclasses.replace(
        fake.dependencies,
        graphs=GraphOpeners(
            native=fake.dependencies.graphs.native,
            baseline=fake.dependencies.graphs.baseline,
            single_tenant=lambda config: opened.append("single_tenant") or fake.single_tenant,  # pyright: ignore[reportUnknownLambdaType, reportUnknownMemberType, reportArgumentType]
        ),
    )

    with pytest.raises(AuditTrailUnavailableError):
        _run(store, deps)

    assert fake.recorder.calls == []
    assert opened == []
    assert store.rows == []


def test_terminal_row_failure_is_logged_with_run_id_and_the_outcome_is_returned_unchanged(
    make_emitter: MakeEmitter, read_lines: ReadLines
) -> None:
    """AC-BI-015: the lost terminal write is logged with the run id; the outcome stands."""
    emitter, log_path = make_emitter()
    store = InMemoryAuditStore()
    original_write = store.record_standalone
    writes = 0

    def _flaky(**kwargs: object) -> None:
        nonlocal writes
        writes += 1
        if writes == 2:
            raise AuditPersistenceError("host=db.internal")
        original_write(**kwargs)  # pyright: ignore[reportArgumentType]

    store.record_standalone = _flaky  # type: ignore[method-assign]  # pyright: ignore[reportAttributeAccessIssue]

    outcome = _run(
        store, build_fake_pipeline_dependencies(rid="CRA-1.0").dependencies, emitter=emitter
    )

    assert getattr(outcome, "regulatory_instrument_id", None) == "CRA-1.0"
    emitter.flush()
    (line,) = [ln for ln in read_lines(log_path) if ln["action"] == "audit_terminal_failed"]
    assert line["run_id"] == _RUN_ID
    assert line["audit_action"] == "ingestion_run.complete"
    assert line["reason"] == "AuditPersistenceError"
    assert "db.internal" not in str(line)


def test_failed_terminal_row_failure_keeps_the_original_pipeline_error() -> None:
    store = InMemoryAuditStore(fail_on_outcome={"failed": AuditPersistenceError("x")})
    deps = build_fake_pipeline_dependencies(
        extract_error=DomainMapperExtractionError("boom")
    ).dependencies

    with pytest.raises(PipelineStageError):
        _run(store, deps)


_FORBIDDEN = re.compile(r"Traceback|/Users|/app|File \"|\.py")


@pytest.mark.parametrize(
    "scenario",
    ["stage_failure", "unexpected", "config", "collision"],
)
def test_failed_rows_contain_no_free_text_error_stack_trace_or_internal_path(
    scenario: str,
) -> None:
    store = InMemoryAuditStore()
    secret = 'File "/Users/dev/x.py", line 3 Traceback /app/svc 10.0.0.1:6379'
    fake = build_fake_pipeline_dependencies(
        extract_error=DomainMapperExtractionError(secret) if scenario == "stage_failure" else None,
        collision_row=("CRA-1.0", "32000R0001") if scenario == "collision" else None,
    )
    deps = fake.dependencies
    config = None
    if scenario == "config":
        config = ServiceConfig(
            host="127.0.0.1", port=8000, graceful_shutdown_seconds=10, logging_dir=None
        )
    if scenario == "unexpected":

        def _boom(_config: object) -> object:
            raise RuntimeError(secret)

        deps = dataclasses.replace(
            deps,
            graphs=GraphOpeners(
                native=deps.graphs.native,
                baseline=deps.graphs.baseline,
                single_tenant=_boom,  # pyright: ignore[reportArgumentType]
            ),
        )

    with pytest.raises(Exception):  # noqa: B017 -- the scenario's own domain error
        _run(store, deps, config=config)

    failed = store.rows[1]
    assert failed.outcome == "failed"
    assert "error" not in failed.details
    assert not _FORBIDDEN.search(repr(failed.details))
