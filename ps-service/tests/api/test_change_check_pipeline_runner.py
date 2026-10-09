"""The sweep's injected `PipelineRunner` (#201 S1c): shared stage sequence, config first, no leaks.

`_build_pipeline_runner` adapts `ingestion_orchestration._execute_catalog_stages` to the
`change_monitor.models.PipelineRunner` shape `trigger_reingestion` is handed. The stages and
graphs are the hand-written `FakePipeline` doubles (the approved stage seam), so these tests
pin the runner's own behaviour: which stages run in which order for which id, that the
pipeline config is checked before any graph is opened, and that the `run_status` registry
never leaks an entry.
"""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING

import pytest

from api._fakes import (
    FakeIngestionAdapter,
    build_fake_change_check_dependencies,
    build_fake_pipeline_dependencies,
)
from ps_service.api import run_status
from ps_service.api.catalog import CatalogEntry
from ps_service.api.change_check_orchestration import (
    _build_pipeline_runner,  # pyright: ignore[reportPrivateUsage]  -- unit under test
    build_default_change_check_dependencies,
)
from ps_service.api.errors import IngestionConfigIncompleteError, PipelineStageError
from ps_service.api.ingestion_orchestration import build_default_pipeline_dependencies
from ps_service.config import ServiceConfig

if TYPE_CHECKING:
    from api._fakes import FakePipeline, MakeEmitter
    from ps_service.api.ingestion_orchestration import GraphHandle
    from ps_service.change_monitor.models import PipelineRunner
    from ps_service.logging import LogEmitter

_ENTRY = CatalogEntry(celex="32024R2847", title="CRA", short_name="CRA", version="1.0")
_NEW_VERSION = "2.0"
_RUN_ID = "reingest-run-1"
_ALL = ("ingestion", "extraction", "derivation", "merge")


def _config(*, complete: bool = True) -> ServiceConfig:
    if not complete:
        return ServiceConfig(
            host="127.0.0.1", port=8000, graceful_shutdown_seconds=10, logging_dir=None
        )
    return ServiceConfig(
        host="127.0.0.1",
        port=8000,
        graceful_shutdown_seconds=10,
        logging_dir=None,
        llm_interface_model="azure/gpt-4o",
        llm_interface_embed_model="azure/text-embedding-3-large",
        company_merge_similarity_threshold=0.83,
    )


def _runner(
    pipeline: FakePipeline,
    config: ServiceConfig,
    *,
    opened: list[str] | None = None,
    emitter: LogEmitter | None = None,
) -> PipelineRunner:
    fake = build_fake_change_check_dependencies(pipeline=pipeline)
    dependencies = fake.dependencies
    if opened is not None:
        graphs = dependencies.pipeline.graphs

        def _counting_baseline(config: ServiceConfig, short_name: str) -> GraphHandle:
            opened.append(short_name)
            return graphs.baseline(config, short_name)

        dependencies = dataclasses.replace(
            dependencies,
            pipeline=dataclasses.replace(
                dependencies.pipeline,
                graphs=dataclasses.replace(graphs, baseline=_counting_baseline),
            ),
        )
    return _build_pipeline_runner(
        _ENTRY,
        _NEW_VERSION,
        config=config,
        adapter=FakeIngestionAdapter(),
        native=pipeline.native,
        single_tenant=pipeline.single_tenant,
        dependencies=dependencies,
        emitter=emitter,
    )


def test_default_change_check_dependencies_carry_the_real_pipeline_stages() -> None:
    assert (
        build_default_change_check_dependencies().pipeline.stages
        == build_default_pipeline_dependencies().stages
    )


def test_runner_runs_the_requested_stages_in_order_and_reports_each_to_the_callback() -> None:
    pipeline = build_fake_pipeline_dependencies(rid="CRA-2.0")
    completed: list[str] = []

    result = _runner(pipeline, _config())(
        stages=_ALL, run_id=_RUN_ID, on_stage_complete=completed.append
    )

    assert pipeline.recorder.order == list(_ALL)
    assert completed == list(_ALL)
    assert [summary.stage for summary in result.stages] == list(_ALL)
    ingest_kwargs = pipeline.recorder.calls[0].kwargs
    assert ingest_kwargs["version"] == _NEW_VERSION
    assert ingest_kwargs["run_id"] == _RUN_ID
    assert ingest_kwargs["identifier"] == _ENTRY.celex


def test_runner_resumed_subset_targets_the_new_version_id_without_running_ingestion() -> None:
    pipeline = build_fake_pipeline_dependencies()
    completed: list[str] = []

    result = _runner(pipeline, _config())(
        stages=("derivation", "merge"), run_id=_RUN_ID, on_stage_complete=completed.append
    )

    assert [(call.stage, call.regulatory_instrument_id) for call in pipeline.recorder.calls] == [
        ("derivation", "CRA-2.0"),
        ("merge", "CRA-2.0"),
    ]
    assert completed == ["derivation", "merge"]
    assert [summary.stage for summary in result.stages] == ["derivation", "merge"]


def test_runner_checks_the_pipeline_config_before_opening_any_graph_or_running_a_stage() -> None:
    pipeline = build_fake_pipeline_dependencies()
    opened: list[str] = []

    with pytest.raises(IngestionConfigIncompleteError):
        _runner(pipeline, _config(complete=False), opened=opened)(
            stages=_ALL, run_id=_RUN_ID, on_stage_complete=lambda _stage: None
        )

    assert opened == []
    assert pipeline.recorder.calls == []
    assert pipeline.native.calls == pipeline.baseline.calls == pipeline.single_tenant.calls == []


def test_runner_clears_the_run_status_entry_on_success_and_on_failure(
    make_emitter: MakeEmitter,
) -> None:
    emitter, _ = make_emitter()
    ok = build_fake_pipeline_dependencies()
    _runner(ok, _config())(stages=_ALL, run_id="ok-run", on_stage_complete=lambda _stage: None)
    assert run_status.get_stage("ok-run") is None

    failing = build_fake_pipeline_dependencies(derive_error=RuntimeError("boom"))
    completed: list[str] = []
    with pytest.raises(PipelineStageError):
        _runner(failing, _config(), emitter=emitter)(
            stages=_ALL, run_id="bad-run", on_stage_complete=completed.append
        )
    assert run_status.get_stage("bad-run") is None
    assert completed == ["ingestion", "extraction"]
