"""Tests for ``ps_service.api.ingestion_orchestration`` (the catalog pipeline sequencer).

Increment 5 (#51a). Drives ``run_catalog_ingestion_pipeline`` with a fake
``PipelineDependencies`` (see ``tests/api/_fakes.py``) so the sequencing, the
config guard, the stage-failure handling, and the ``ingestion_run`` log entries
are all exercised without a real graph, adapter, or LLM.

AC coverage: AC-BI-002 (chained stages), AC-BI-003 (each stage consumes the
prior id), AC-BI-008 (failure aborts + names the stage), AC-BI-009 (unsafe stage
message is sanitised), AC-BI-011 (run log records id / source / caller / start /
end), plus the 503 config guard.
"""

from __future__ import annotations

import pytest

from api._fakes import MakeEmitter, ReadLines, build_fake_pipeline_dependencies
from ps_service.api.catalog import CatalogEntry
from ps_service.api.errors import IngestionConfigIncompleteError, PipelineStageError
from ps_service.api.ingestion_orchestration import run_catalog_ingestion_pipeline
from ps_service.config import ServiceConfig
from ps_service.domain_mapper.errors import DomainMapperExtractionError
from ps_service.ingestion.falkordb_client import FalkorDBConnectionError

_CELEX = "32024R2847"
_ENTRY = CatalogEntry(_CELEX, "Cyber Resilience Act", "CRA", "1.0")


def _complete_config() -> ServiceConfig:
    """A fully-populated config: LLM model, embed model, and similarity threshold all set."""
    return ServiceConfig(
        host="127.0.0.1",
        port=8000,
        graceful_shutdown_seconds=10,
        logging_dir=None,
        llm_interface_model="azure/gpt-4o",
        llm_interface_embed_model="azure/text-embedding-3-large",
        company_merge_similarity_threshold=0.83,
    )


def _incomplete_config() -> ServiceConfig:
    """A config with no LLM model / embed model / similarity threshold."""
    return ServiceConfig(
        host="127.0.0.1", port=8000, graceful_shutdown_seconds=10, logging_dir=None
    )


def test_catalog_pipeline_runs_ingest_extract_derive_merge_in_order(
    make_emitter: MakeEmitter,
) -> None:
    """AC-BI-002: the four stages run once each, in pipeline order."""
    emitter, _ = make_emitter()
    fake = build_fake_pipeline_dependencies(rid="CRA-1.0")

    outcome = run_catalog_ingestion_pipeline(
        _ENTRY,
        config=_complete_config(),
        run_id="run-1",
        caller="127.0.0.1",
        dependencies=fake.dependencies,
        emitter=emitter,
    )

    assert fake.recorder.order == ["ingestion", "extraction", "derivation", "merge"]
    assert outcome.source == "catalog"
    assert outcome.regulatory_instrument_id == "CRA-1.0"
    assert [report.stage for report in outcome.stages] == [
        "ingestion",
        "extraction",
        "derivation",
        "merge",
    ]


def test_each_stage_consumes_prior_stage_regulatory_instrument_id(
    make_emitter: MakeEmitter,
) -> None:
    """AC-BI-003: extraction / derivation / merge all use the id the ingest stage returned."""
    emitter, _ = make_emitter()
    fake = build_fake_pipeline_dependencies(rid="GDPR-1.0")

    run_catalog_ingestion_pipeline(
        _ENTRY,
        config=_complete_config(),
        run_id="run-2",
        caller="127.0.0.1",
        dependencies=fake.dependencies,
        emitter=emitter,
    )

    downstream = [call for call in fake.recorder.calls if call.stage != "ingestion"]
    assert [call.regulatory_instrument_id for call in downstream] == [
        "GDPR-1.0",
        "GDPR-1.0",
        "GDPR-1.0",
    ]


def test_stage_failure_aborts_and_names_failing_stage(make_emitter: MakeEmitter) -> None:
    """AC-BI-008: a stage error surfaces as PipelineStageError naming that stage."""
    emitter, _ = make_emitter()
    fake = build_fake_pipeline_dependencies(
        extract_error=DomainMapperExtractionError("unit 7 produced no candidates")
    )

    with pytest.raises(PipelineStageError) as exc_info:
        run_catalog_ingestion_pipeline(
            _ENTRY,
            config=_complete_config(),
            run_id="run-3",
            caller="127.0.0.1",
            dependencies=fake.dependencies,
            emitter=emitter,
        )

    assert exc_info.value.stage == "extraction"
    assert "unit 7 produced no candidates" in exc_info.value.reason


def test_stage_failure_prevents_later_stages_from_running(make_emitter: MakeEmitter) -> None:
    """AC-BI-008: derivation and merge never run once extraction has failed."""
    emitter, _ = make_emitter()
    fake = build_fake_pipeline_dependencies(extract_error=DomainMapperExtractionError("boom"))

    with pytest.raises(PipelineStageError):
        run_catalog_ingestion_pipeline(
            _ENTRY,
            config=_complete_config(),
            run_id="run-4",
            caller="127.0.0.1",
            dependencies=fake.dependencies,
            emitter=emitter,
        )

    assert fake.recorder.order == ["ingestion", "extraction"]


def test_incomplete_llm_config_raises_503_before_any_stage_runs() -> None:
    """The config guard raises IngestionConfigIncompleteError (503) before any stage runs."""
    fake = build_fake_pipeline_dependencies()

    with pytest.raises(IngestionConfigIncompleteError):
        run_catalog_ingestion_pipeline(
            _ENTRY,
            config=_incomplete_config(),
            run_id="run-5",
            caller="127.0.0.1",
            dependencies=fake.dependencies,
        )

    assert fake.recorder.order == []


def test_unsafe_stage_exception_is_sanitized_to_generic_reason(
    make_emitter: MakeEmitter,
) -> None:
    """AC-BI-009: a non-whitelisted stage error collapses to a generic, host-free reason."""
    emitter, _ = make_emitter()
    fake = build_fake_pipeline_dependencies(
        merge_error=FalkorDBConnectionError("FalkorDB connection failed at 10.0.0.5:6379")
    )

    with pytest.raises(PipelineStageError) as exc_info:
        run_catalog_ingestion_pipeline(
            _ENTRY,
            config=_complete_config(),
            run_id="run-6",
            caller="127.0.0.1",
            dependencies=fake.dependencies,
            emitter=emitter,
        )

    assert exc_info.value.stage == "merge"
    assert exc_info.value.reason == "merge failed"
    assert "10.0.0.5" not in exc_info.value.reason
    assert "6379" not in exc_info.value.reason


def test_emits_started_and_succeeded_entries_with_run_id_source_and_caller_and_duration(
    make_emitter: MakeEmitter,
    read_lines: ReadLines,
) -> None:
    """AC-BI-011: the run log records the run id, source id, caller, and start/end."""
    emitter, log_path = make_emitter()
    fake = build_fake_pipeline_dependencies()

    run_catalog_ingestion_pipeline(
        _ENTRY,
        config=_complete_config(),
        run_id="run-xyz",
        caller="10.1.2.3",
        dependencies=fake.dependencies,
        emitter=emitter,
    )
    emitter.flush()

    runs = [line for line in read_lines(log_path) if line.get("action") == "ingestion_run"]
    assert [run["outcome"] for run in runs] == ["started", "succeeded"]
    assert all(run["run_id"] == "run-xyz" for run in runs)
    assert all(run["source_identifier"] == _CELEX for run in runs)
    assert all(run["caller"] == "10.1.2.3" for run in runs)
    assert "duration_ms" not in runs[0]
    assert isinstance(runs[1]["duration_ms"], float)
