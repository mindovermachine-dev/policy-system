"""Tests for ``ps_service.api.ingestion_orchestration`` (the catalog pipeline sequencer).

Drives ``run_catalog_ingestion_pipeline`` with a fake
``PipelineDependencies`` (see ``tests/api/_fakes.py``) so the sequencing, the
config guard, the stage-failure handling, and the ``ingestion_run`` log entries
are all exercised without a real graph, adapter, or LLM.

Covers: chained stages (each stage consumes the prior id), a failure aborting and
naming the stage, an unsafe stage message being sanitised, the run log recording
id / source / caller / start / end, and the 503 config guard.
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, cast

import pytest
from company_merge._thresholds import REVIEW_THRESHOLD, ROLE_REVIEW_THRESHOLD

from api._fakes import (
    FakeGraphHandle,
    FakeIngestionAdapter,
    FakePipeline,
    FakeQueryResult,
    MakeEmitter,
    ReadLines,
    build_fake_pipeline_dependencies,
    default_cellar_rdf,
    default_cellar_xhtml,
)
from ps_service.api.catalog import CatalogEntry
from ps_service.api.errors import (
    CatalogIdentifierNotFoundError,
    CelexAlreadyIngestedError,
    IngestionConfigIncompleteError,
    PipelineStageError,
    ShortNameCollisionError,
)
from ps_service.api.graph_write_access import graph_write_access
from ps_service.api.ingestion_orchestration import (
    _STAGE_ORDER,  # pyright: ignore[reportPrivateUsage] — internal constant under test
    StageReport,
    _classify_stage_failure,  # pyright: ignore[reportPrivateUsage] — internal helper under test
    _derive_short_name,  # pyright: ignore[reportPrivateUsage] — internal helper under test
    _execute_catalog_stages,  # pyright: ignore[reportPrivateUsage] — internal helper under test
    _extraction_summary,  # pyright: ignore[reportPrivateUsage] — internal helper under test
    _internal_ingestion_summary,  # pyright: ignore[reportPrivateUsage] — internal helper under test
    _is_already_merged,  # pyright: ignore[reportPrivateUsage] — internal helper under test
    _merge_summary,  # pyright: ignore[reportPrivateUsage] — internal helper under test
    _OpenGraphs,  # pyright: ignore[reportPrivateUsage] — internal helper under test
    _require_ingestion_config,  # pyright: ignore[reportPrivateUsage] — internal helper under test
    check_short_name_collision,
    classify_ingestion_failure,
    normalize_short_name,
    resolve_ingestion_entry,
    resolve_via_cellar,
    run_catalog_ingestion_pipeline,
)
from ps_service.api.run_status import get_stage
from ps_service.change_monitor.errors import ChangeMonitorStateError, SuccessionPersistenceError
from ps_service.change_monitor.models import PIPELINE_STAGES
from ps_service.company_merge.models import MergeResult, NearMissPair
from ps_service.config import ServiceConfig
from ps_service.dependency_health import CELLAR_ELI, is_healthy
from ps_service.domain_mapper.errors import (
    DomainMapperExtractionError,
    DomainMapperPersistenceError,
)
from ps_service.domain_mapper.models import DerivationResult, ExtractionResult
from ps_service.ingestion.adapters.errors import CellarFetchError, CellarNotFoundError
from ps_service.ingestion.adapters.internal_seed.persist import InternalIngestResult
from ps_service.ingestion.falkordb_client import FalkorDBConnectionError
from ps_service.ingestion.models import IngestResult
from ps_service.mcp_interface.errors import McpGraphUnavailableError

if TYPE_CHECKING:
    from pathlib import Path

    from api._gateway_fakes import ProcessGateway
    from ps_service.api.ingestion_orchestration import GraphHandle
    from ps_service.ingestion.adapters.base import IngestionAdapter
    from ps_service.ingestion.graph_submission import GroupSubmitter
    from ps_service.llm_interface.client import CompletionCaller, EmbeddingCaller
    from ps_service.logging import LogEmitter

_CELEX = "32024R2847"
_ENTRY = CatalogEntry(_CELEX, "Cyber Resilience Act", "CRA", "1.0")

_NONCURATED_CELEX = "32020R1111"

# A Regulation-shaped Cellar XHTML fixture, mirroring
# tests/ingestion/adapters/cellar_eli/test_adapter.py's Fixture A.
_FIXTURE_REGULATION_A = b"""
<html xmlns="http://www.w3.org/1999/xhtml">
<body>
<div class="eli-container" id="enc_1">
<div class="eli-main-title">Regulation (EU) 1111/1111 Fixture A</div>
<div class="eli-subdivision" id="cpt_I">
<div class="eli-title" id="cpt_I.tit_1">CHAPTER I General provisions</div>
<div class="eli-subdivision" id="art_1">
<div class="eli-title" id="art_1.tit_1">Article 1 Entry into force and application</div>
<div>This Regulation shall enter into force on the twentieth day following
publication. It shall apply from 1 January 2030.</div>
</div>
</div>
</div>
</body>
</html>
"""

# Same structure as Fixture A but with no `eli-main-title` div at all -- title
# resolves to "", which `RegulatoryInstrumentMetadata`'s `Field(min_length=1)`
# rejects, so `extract_metadata` raises `pydantic.ValidationError` for this
# shape (verified directly; it does not silently return an empty title).
_FIXTURE_NO_TITLE = b"""
<html xmlns="http://www.w3.org/1999/xhtml">
<body>
<div class="eli-container" id="enc_1">
<div class="eli-subdivision" id="cpt_I">
<div class="eli-title" id="cpt_I.tit_1">CHAPTER I General provisions</div>
<div class="eli-subdivision" id="art_1">
<div class="eli-title" id="art_1.tit_1">Article 1 Entry into force and application</div>
<div>This Regulation shall enter into force on the twentieth day following
publication. It shall apply from 1 January 2030.</div>
</div>
</div>
</div>
</body>
</html>
"""

# No resolvable effective_date -- extract_metadata raises CellarParseError.
_FIXTURE_UNRESOLVABLE_EFFECTIVE_DATE = b"""
<html xmlns="http://www.w3.org/1999/xhtml">
<body>
<div class="eli-container" id="enc_1">
<div class="eli-main-title">Some Regulation</div>
<div class="eli-subdivision" id="art_1">
<div class="eli-title" id="art_1.tit_1">Article 1 Subject matter</div>
<div id="001.001">This Regulation establishes rules.</div>
</div>
</div>
</body>
</html>
"""

# RDF own-subject fixture for `_NONCURATED_CELEX` ("32020R1111") -- own
# subject asserts both `resource_legal_id_celex` and `date_entry-into-force`,
# so `extract_metadata` resolves `effective_date` successfully. Used
# wherever a test needs `resolve_via_cellar` to reach its happy path (or a
# post-RDF failure, e.g. the empty-title case) rather than fail on RDF
# resolution itself.
_RDF_FIXTURE_REGULATION_A = b"""<rdf:RDF
    xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
    xmlns:j.0="http://publications.europa.eu/ontology/cdm#">
<rdf:Description rdf:about="http://publications.europa.eu/resource/celex/32020R1111">
<j.0:resource_legal_id_celex rdf:datatype="http://www.w3.org/2001/XMLSchema#string">32020R1111</j.0:resource_legal_id_celex>
<j.0:date_entry-into-force rdf:datatype="http://www.w3.org/2001/XMLSchema#date">2030-01-01</j.0:date_entry-into-force>
</rdf:Description>
</rdf:RDF>
"""

# No subject at all -- no admissible RDF subject asserts the instrument
# type's target predicate, so `extract_metadata` raises `CellarParseError`.
_RDF_FIXTURE_EMPTY = b"""<rdf:RDF
    xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
</rdf:RDF>
"""


class _CountingFetch:
    """A fake `cellar_fetch`/`cellar_fetch_rdf` that returns a fixed body and
    counts its own calls -- shape-agnostic (XHTML vs RDF/XML) since it only
    ever returns whatever `body` it was constructed with; used as both the
    XHTML and the RDF fetch fake, one instance per document, so each has its
    own independent call count.
    """

    def __init__(self, body: bytes) -> None:
        self.call_count = 0
        self._body = body

    def __call__(self, celex: str) -> bytes:
        self.call_count += 1
        return self._body


def _complete_config() -> ServiceConfig:
    """A fully-populated config: LLM model, embed model, and merge threshold all set."""
    return ServiceConfig(
        host="127.0.0.1",
        port=8000,
        graceful_shutdown_seconds=10,
        logging_dir=None,
        llm_interface_model="azure/gpt-4o",
        llm_interface_embed_model="azure/text-embedding-3-large",
        company_merge_capability_merge_threshold=0.83,
        company_merge_capability_review_threshold=REVIEW_THRESHOLD,
        company_merge_role_review_threshold=ROLE_REVIEW_THRESHOLD,
    )


def _incomplete_config() -> ServiceConfig:
    """A config with no LLM model / embed model / merge threshold."""
    return ServiceConfig(
        host="127.0.0.1", port=8000, graceful_shutdown_seconds=10, logging_dir=None
    )


def test_derive_short_name_slugifies_title_words_and_appends_lowercase_celex() -> None:
    """The slug is the title's words, lowercased and `_`-joined, with the
    lowercase CELEX appended for uniqueness.
    """
    result = _derive_short_name("Regulation (EU) 2019/881 of 17 April 2019 on ENISA", "32019R0881")

    assert result == "regulation_eu_2019_881_of_17_32019r0881"


def test_derive_short_name_truncates_to_max_six_words() -> None:
    """A title longer than six words still yields exactly six words, plus the CELEX."""
    title = "One Two Three Four Five Six Seven Eight Nine"

    result = _derive_short_name(title, "32019R0881")

    assert result == "one_two_three_four_five_six_32019r0881"


def test_derive_short_name_is_deterministic() -> None:
    title = "Regulation (EU) 2019/881 of 17 April 2019 on ENISA"

    assert _derive_short_name(title, "32019R0881") == _derive_short_name(title, "32019R0881")


def test_catalog_pipeline_runs_ingest_extract_derive_merge_in_order(
    make_emitter: MakeEmitter,
) -> None:
    """The four stages run once each, in pipeline order."""
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

    expected_order = ["ingestion", "extraction", "derivation", "merge"]
    assert fake.recorder.order == expected_order
    assert outcome.source == "catalog"
    assert outcome.regulatory_instrument_id == "CRA-1.0"
    assert [report.stage for report in outcome.stages] == [
        "ingestion",
        "extraction",
        "derivation",
        "merge",
    ]


def test_extract_and_derive_stages_receive_the_gateway_baseline_graph_name_and_run_audit_event(
    make_emitter: MakeEmitter, process_gateway: ProcessGateway
) -> None:
    """The baseline-writing stages get the same gateway and run audit
    event the ingest stage got, and the `{short}_baseline` graph name; one audit row per run.
    """
    emitter, _ = make_emitter()
    fake = build_fake_pipeline_dependencies(rid="CRA-1.0")

    run_catalog_ingestion_pipeline(
        _ENTRY,
        config=_complete_config(),
        run_id="run-1",
        caller="127.0.0.1",
        dependencies=fake.dependencies,
        emitter=emitter,
    )

    ingest, extract, derive = (fake.recorder.calls[index].kwargs for index in range(3))
    (audit_event_id,) = process_gateway.system_audit.ids
    assert ingest["audit_event_id"] == audit_event_id
    for stage_kwargs in (extract, derive):
        assert stage_kwargs["submitter"] is process_gateway.rig.gateway
        assert stage_kwargs["baseline_graph_name"] == "cra_baseline"
        assert stage_kwargs["audit_event_id"] == audit_event_id


def test_derivation_stage_summary_reports_unmatched_obligations_count(
    make_emitter: MakeEmitter,
) -> None:
    """The derivation stage's ``StageReport.summary`` dict
    carries an ``unmatched_obligations`` count, symmetric with the existing
    ``unmatched_requirements`` key, sourced from
    ``DerivationResult.unmatched_obligation_ids``.
    """
    emitter, _ = make_emitter()
    fake = build_fake_pipeline_dependencies(
        rid="CRA-1.0",
        derive_unmatched_obligation_ids=("obl_conduct_risk_assessment_aaaaaa",),
    )

    outcome = run_catalog_ingestion_pipeline(
        _ENTRY,
        config=_complete_config(),
        run_id="run-unmatched-obligations",
        caller="127.0.0.1",
        dependencies=fake.dependencies,
        emitter=emitter,
    )

    derivation_report = next(report for report in outcome.stages if report.stage == "derivation")
    assert derivation_report.summary["unmatched_obligations"] == 1
    assert derivation_report.summary["unmatched_requirements"] == 0


def test_each_stage_consumes_prior_stage_regulatory_instrument_id(
    make_emitter: MakeEmitter,
) -> None:
    """Extraction / derivation / merge all use the id the ingest stage returned."""
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
    """A stage error surfaces as PipelineStageError naming that stage."""
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


def test_a_persistence_error_from_the_extract_stage_keeps_its_classification(
    make_emitter: MakeEmitter,
) -> None:
    """The baseline write fails as a gateway refusal, but the stage
    error is still classified as a `PipelineStageError` naming `extraction`, whose
    caller-visible reason is the generic `extraction failed` (the persistence error is not on
    the pass-through whitelist, like a FalkorDB write error), and no later stage runs.
    """
    emitter, _ = make_emitter()
    persistence_error = DomainMapperPersistenceError(
        "FalkorDB write failed: the graph log could not be written"
    )
    fake = build_fake_pipeline_dependencies(extract_error=persistence_error)

    with pytest.raises(PipelineStageError) as exc_info:
        run_catalog_ingestion_pipeline(
            _ENTRY,
            config=_complete_config(),
            run_id="run-3p",
            caller="127.0.0.1",
            dependencies=fake.dependencies,
            emitter=emitter,
        )

    assert exc_info.value.stage == "extraction"
    assert exc_info.value.reason == "extraction failed"
    assert exc_info.value.__cause__ is persistence_error
    assert fake.recorder.order == ["ingestion", "extraction"]


def test_stage_failure_prevents_later_stages_from_running(make_emitter: MakeEmitter) -> None:
    """Derivation and merge never run once extraction has failed."""
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


def test_unset_review_threshold_raises_503_naming_it_before_any_stage_runs() -> None:
    fake = build_fake_pipeline_dependencies()

    with pytest.raises(IngestionConfigIncompleteError) as raised:
        run_catalog_ingestion_pipeline(
            _ENTRY,
            config=replace(_complete_config(), company_merge_capability_review_threshold=None),
            run_id="run-5",
            caller="127.0.0.1",
            dependencies=fake.dependencies,
        )

    assert "company_merge_capability_review_threshold" in str(raised.value)
    assert fake.recorder.order == []


def test_unset_role_review_threshold_raises_503_naming_it_before_any_stage_runs() -> None:
    fake = build_fake_pipeline_dependencies()

    with pytest.raises(IngestionConfigIncompleteError) as raised:
        run_catalog_ingestion_pipeline(
            _ENTRY,
            config=replace(_complete_config(), company_merge_role_review_threshold=None),
            run_id="run-5",
            caller="127.0.0.1",
            dependencies=fake.dependencies,
        )

    assert "company_merge_role_review_threshold" in str(raised.value)
    assert fake.recorder.order == []


def test_merge_stage_receives_the_configured_merge_and_review_thresholds(
    make_emitter: MakeEmitter,
) -> None:
    emitter, _ = make_emitter()
    fake = build_fake_pipeline_dependencies(rid="CRA-1.0")
    config = replace(
        _complete_config(),
        company_merge_capability_merge_threshold=0.9,
        company_merge_capability_review_threshold=0.4,
        company_merge_role_review_threshold=0.7,
    )

    run_catalog_ingestion_pipeline(
        _ENTRY,
        config=config,
        run_id="run-1",
        caller="127.0.0.1",
        dependencies=fake.dependencies,
        emitter=emitter,
    )

    merge_kwargs = next(call.kwargs for call in fake.recorder.calls if call.stage == "merge")
    assert merge_kwargs["merge_threshold"] == 0.9
    assert merge_kwargs["review_threshold"] == 0.4
    assert merge_kwargs["role_review_threshold"] == 0.7


def test_catalog_merge_targets_the_configured_single_tenant_graph(
    make_emitter: MakeEmitter,
) -> None:
    emitter, _ = make_emitter()
    fake = build_fake_pipeline_dependencies(rid="CRA-1.0")

    run_catalog_ingestion_pipeline(
        _ENTRY,
        config=replace(_complete_config(), falkordb_graph="custom_graph"),
        run_id="run-custom-graph",
        caller="127.0.0.1",
        dependencies=fake.dependencies,
        emitter=emitter,
    )

    merge_kwargs = next(call.kwargs for call in fake.recorder.calls if call.stage == "merge")
    assert merge_kwargs["single_tenant_graph_name"] == "custom_graph"


def test_unsafe_stage_exception_is_sanitized_to_generic_reason(
    make_emitter: MakeEmitter,
) -> None:
    """A non-whitelisted stage error collapses to a generic, host-free reason."""
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
    """The run log records the run id, source id, caller, and start/end."""
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


def test_succeeded_ingestion_run_log_carries_trigger_and_counts(
    make_emitter: MakeEmitter,
    read_lines: ReadLines,
) -> None:
    """The succeeded log line agrees with the audit row (trigger + the three counts)."""
    emitter, log_path = make_emitter()
    fake = build_fake_pipeline_dependencies()

    run_catalog_ingestion_pipeline(
        _ENTRY,
        config=_complete_config(),
        run_id="run-xyz",
        caller="10.1.2.3",
        dependencies=fake.dependencies,
        emitter=emitter,
        trigger="sync_ingest",
    )
    emitter.flush()

    runs = [line for line in read_lines(log_path) if line.get("action") == "ingestion_run"]
    succeeded = next(run for run in runs if run["outcome"] == "succeeded")
    assert succeeded["trigger"] == "sync_ingest"
    assert (
        succeeded["new_obligations"],
        succeeded["new_capabilities"],
        succeeded["matched_capabilities"],
    ) == (0, 0, 0)
    assert "trigger" not in next(run for run in runs if run["outcome"] == "started")


def test_ingestion_run_log_without_a_trigger_is_unchanged(
    make_emitter: MakeEmitter,
    read_lines: ReadLines,
) -> None:
    emitter, log_path = make_emitter()
    fake = build_fake_pipeline_dependencies()

    run_catalog_ingestion_pipeline(
        _ENTRY,
        config=_complete_config(),
        run_id="run-xyz",
        caller="c",
        dependencies=fake.dependencies,
        emitter=emitter,
    )
    emitter.flush()

    runs = [line for line in read_lines(log_path) if line.get("action") == "ingestion_run"]
    assert all("trigger" not in run and "new_obligations" not in run for run in runs)


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (IngestionConfigIncompleteError("x"), "config_incomplete"),
        (CatalogIdentifierNotFoundError("x"), "celex_not_found"),
        (ShortNameCollisionError("x"), "short_name_collision"),
        (PipelineStageError(stage="merge", reason="boom /Users/x"), "pipeline_stage_failed"),
        (ChangeMonitorStateError("no single active prior"), "inconsistent_graph_state"),
        (
            SuccessionPersistenceError("FalkorDB write failed: 10.0.0.1"),
            "graph_unavailable",
        ),
        (RuntimeError("boom"), "unexpected_error"),
    ],
)
def test_classify_ingestion_failure_maps_the_exception_type_to_a_reason_code(
    exc: Exception, expected: str
) -> None:
    assert classify_ingestion_failure(exc) == expected


def test_classify_ingestion_failure_matches_the_graph_unavailable_error_by_class_name() -> None:
    assert classify_ingestion_failure(McpGraphUnavailableError()) == "graph_unavailable"


def test_classify_stage_failure_returns_scrubbed_verbatim_reason_for_whitelisted_exception() -> (
    None
):
    """A whitelisted domain exception's message is surfaced verbatim (scrubbed, truncated)."""
    exc = DomainMapperExtractionError("unit 7 produced no candidates")

    result = _classify_stage_failure("extraction", exc)

    assert result.stage == "extraction"
    assert "unit 7 produced no candidates" in result.reason


def test_classify_stage_failure_returns_generic_reason_for_unwhitelisted_exception(
    make_emitter: MakeEmitter,
) -> None:
    """A non-whitelisted exception collapses to a generic, host-free reason, with the
    full detail logged server-side only.
    """
    emitter, _ = make_emitter()
    exc = FalkorDBConnectionError("FalkorDB connection failed at 10.0.0.5:6379")

    result = _classify_stage_failure("merge", exc, emitter=emitter)

    assert result.stage == "merge"
    assert result.reason == "merge failed"
    assert "10.0.0.5" not in result.reason


# --- injected ingestion_adapter ----------------------------------------------


def test_run_catalog_ingestion_pipeline_uses_injected_ingestion_adapter_when_provided(
    make_emitter: MakeEmitter,
) -> None:
    """A caller-supplied ``ingestion_adapter`` is passed to the ingest stage
    verbatim, not the dependency-bundle's default factory.
    """
    emitter, _ = make_emitter()
    fake = build_fake_pipeline_dependencies()
    sentinel = cast("IngestionAdapter", object())

    run_catalog_ingestion_pipeline(
        _ENTRY,
        config=_complete_config(),
        run_id="run-7a",
        caller="127.0.0.1",
        dependencies=fake.dependencies,
        emitter=emitter,
        ingestion_adapter=sentinel,
    )

    assert fake.recorder.calls[0].kwargs["adapter"] is sentinel


def test_run_catalog_ingestion_pipeline_falls_back_to_default_adapter_when_omitted(
    make_emitter: MakeEmitter,
) -> None:
    """Regression: omitting ``ingestion_adapter`` still uses the dependency bundle's
    default factory, exactly as today's curated path does.
    """
    emitter, _ = make_emitter()
    fake = build_fake_pipeline_dependencies()

    run_catalog_ingestion_pipeline(
        _ENTRY,
        config=_complete_config(),
        run_id="run-7b",
        caller="127.0.0.1",
        dependencies=fake.dependencies,
        emitter=emitter,
    )

    assert isinstance(fake.recorder.calls[0].kwargs["adapter"], FakeIngestionAdapter)


# A real curated CELEX, used as an ordinary CELEX by the `resolve_ingestion_entry` tests
# below (the catalog is never consulted by the ingest path).
_CURATED_CELEX = "32024R2847"


# --- resolve_ingestion_entry -------------------------------------------------


def test_resolve_ingestion_entry_resolves_a_curated_celex_via_cellar_under_the_callers_short_name(
    make_emitter: MakeEmitter,
) -> None:
    """A curated CELEX is resolved through Cellar -- both documents
    fetched once -- under the caller's own ``short_name``, after exactly one graph query
    (the short_name collision check). The returned resolution carries the entry and the
    fetch-once adapter.
    """
    graph = FakeGraphHandle()
    emitter, _ = make_emitter()
    fetch = _CountingFetch(default_cellar_xhtml(_CURATED_CELEX))
    fetch_rdf = _CountingFetch(default_cellar_rdf(_CURATED_CELEX))

    resolution = resolve_ingestion_entry(
        _CURATED_CELEX,
        "mycra",
        single_tenant_graph=graph,
        emitter=emitter,
        cellar_fetch=fetch,
        cellar_fetch_rdf=fetch_rdf,
    )

    assert resolution.entry.celex == _CURATED_CELEX
    assert resolution.entry.short_name == "MYCRA"
    assert (fetch.call_count, fetch_rdf.call_count) == (1, 1)
    assert graph.calls[-1].params == {"prefix": "MYCRA-", "celex": _CURATED_CELEX}


def test_resolve_ingestion_entry_rejects_a_claimed_short_name_before_any_cellar_fetch(
    make_emitter: MakeEmitter,
) -> None:
    """Ordering: the graph collision check runs before Cellar, so a doomed
    request never reaches the network.
    """
    graph = FakeGraphHandle([FakeQueryResult([]), FakeQueryResult([["mycra-1.0", "32024R0001"]])])
    emitter, _ = make_emitter()
    fetch = _CountingFetch(_FIXTURE_REGULATION_A)
    fetch_rdf = _CountingFetch(_RDF_FIXTURE_REGULATION_A)

    with pytest.raises(ShortNameCollisionError) as exc_info:
        resolve_ingestion_entry(
            _CURATED_CELEX,
            "mycra",
            single_tenant_graph=graph,
            emitter=emitter,
            cellar_fetch=fetch,
            cellar_fetch_rdf=fetch_rdf,
        )

    assert str(exc_info.value) == "short_name 'MYCRA' is already claimed by CELEX 32024R0001"
    assert (fetch.call_count, fetch_rdf.call_count) == (0, 0)


def test_resolve_ingestion_entry_collision_check_failure_fails_closed_before_cellar(
    make_emitter: MakeEmitter,
) -> None:
    """A graph failure in the collision check is a
    ``PipelineStageError`` naming ``collision_check``, never "no collision", and Cellar
    is never reached.
    """
    graph = build_fake_pipeline_dependencies(collision_error=RuntimeError("boom")).single_tenant
    emitter, _ = make_emitter()
    fetch = _CountingFetch(_FIXTURE_REGULATION_A)

    with pytest.raises(PipelineStageError) as exc_info:
        resolve_ingestion_entry(
            _CURATED_CELEX,
            "mycra",
            single_tenant_graph=graph,
            emitter=emitter,
            cellar_fetch=fetch,
        )

    assert exc_info.value.stage == "collision_check"
    assert fetch.call_count == 0


def test_resolve_ingestion_entry_not_found_message_names_the_celex_without_the_catalog(
    make_emitter: MakeEmitter,
) -> None:
    """A CELEX Cellar does not know raises the not-found error
    naming the CELEX; the message does not claim a curated catalog was consulted.
    """

    def _not_found(celex: str) -> bytes:
        raise CellarNotFoundError(f"CELEX {celex!r} was not found on Cellar/ELI")

    emitter, _ = make_emitter()

    with pytest.raises(CatalogIdentifierNotFoundError) as exc_info:
        resolve_ingestion_entry(
            _CURATED_CELEX,
            "mycra",
            single_tenant_graph=FakeGraphHandle(),
            emitter=emitter,
            cellar_fetch=_not_found,
        )

    assert _CURATED_CELEX in str(exc_info.value)
    assert "curated" not in str(exc_info.value)


def test_resolve_ingestion_entry_checks_celex_then_collision_then_cellar(
    make_emitter: MakeEmitter,
) -> None:
    """Ordering: CELEX-existence query first, then the short_name collision
    query, then Cellar -- a request doomed by the graph never reaches the network.
    """
    graph = FakeGraphHandle()
    emitter, _ = make_emitter()
    fetch = _CountingFetch(default_cellar_xhtml(_CURATED_CELEX))
    fetch_rdf = _CountingFetch(default_cellar_rdf(_CURATED_CELEX))

    resolve_ingestion_entry(
        _CURATED_CELEX,
        "mycra",
        single_tenant_graph=graph,
        emitter=emitter,
        cellar_fetch=fetch,
        cellar_fetch_rdf=fetch_rdf,
    )

    assert [call.params for call in graph.calls] == [
        {"celex": _CURATED_CELEX},
        {"prefix": "MYCRA-", "celex": _CURATED_CELEX},
    ]


def test_resolve_ingestion_entry_rejects_an_ingested_celex_before_collision_check_and_cellar(
    make_emitter: MakeEmitter,
) -> None:
    """An already-ingested CELEX raises ``CelexAlreadyIngestedError``
    naming the existing short_name, with no collision query and no Cellar fetch.
    """
    graph = FakeGraphHandle([FakeQueryResult([["cra-1.0"]])])
    emitter, _ = make_emitter()
    fetch = _CountingFetch(_FIXTURE_REGULATION_A)

    with pytest.raises(CelexAlreadyIngestedError) as exc_info:
        resolve_ingestion_entry(
            _CURATED_CELEX,
            "mycra",
            single_tenant_graph=graph,
            emitter=emitter,
            cellar_fetch=fetch,
        )

    assert str(exc_info.value) == f"CELEX {_CURATED_CELEX} is already ingested as short_name 'cra'"
    assert len(graph.calls) == 1
    assert fetch.call_count == 0


def test_resolve_ingestion_entry_celex_check_failure_fails_closed_naming_celex_check(
    make_emitter: MakeEmitter,
) -> None:
    """A graph failure in the CELEX check is a ``PipelineStageError``
    with stage ``celex_check`` (the collision check keeps ``collision_check``).
    """
    graph = FakeGraphHandle(error=RuntimeError("boom"))
    emitter, _ = make_emitter()

    with pytest.raises(PipelineStageError) as exc_info:
        resolve_ingestion_entry(_CURATED_CELEX, "mycra", single_tenant_graph=graph, emitter=emitter)

    assert exc_info.value.stage == "celex_check"


def test_resolve_ingestion_entry_logs_celex_rejection_with_only_safe_fields(
    make_emitter: MakeEmitter, read_lines: ReadLines
) -> None:
    """The rejection is logged as ``ingestion_identity_check``
    with outcome ``rejected_celex_exists`` carrying only the CELEX, short_name and the
    existing short_name -- no Cypher, no exception text.
    """
    graph = FakeGraphHandle([FakeQueryResult([["cra-1.0"]])])
    emitter, log_path = make_emitter()

    with pytest.raises(CelexAlreadyIngestedError):
        resolve_ingestion_entry(_CURATED_CELEX, "mycra", single_tenant_graph=graph, emitter=emitter)

    emitter.flush()

    entries = [e for e in read_lines(log_path) if e.get("action") == "ingestion_identity_check"]
    assert len(entries) == 1
    assert entries[0]["outcome"] == "rejected_celex_exists"
    assert entries[0]["celex"] == _CURATED_CELEX
    assert entries[0]["short_name"] == "MYCRA"
    assert entries[0]["existing_short_name"] == "cra"
    assert "MATCH" not in str(entries[0])


# --- short_name normalization + case-insensitivity ---------------------------


def test_check_short_name_collision_compares_case_insensitively() -> None:
    """``cra`` matches a recorded ``CRA-1.0`` (and the reverse); the
    Python compare is on the upper-cased short name segment.
    """
    graph = FakeGraphHandle(
        [FakeQueryResult([["CRA-1.0", "32024R0001"]]), FakeQueryResult([["cra-1.0", "32024R0001"]])]
    )

    lower_vs_upper = check_short_name_collision(
        cast("GraphHandle", graph), celex="32024R2847", short_name="cra"
    )
    upper_vs_lower = check_short_name_collision(
        cast("GraphHandle", graph), celex="32024R2847", short_name="CRA"
    )

    assert lower_vs_upper == "32024R0001"
    assert upper_vs_lower == "32024R0001"


def test_collision_check_param_prefix_is_uppercase_and_query_is_case_insensitive() -> None:
    """The collision query upper-cases the stored id
    (``toUpper(n.id)``) and is parameterized with an upper-case prefix, whatever case the
    caller passed.
    """
    graph = FakeGraphHandle()

    check_short_name_collision(cast("GraphHandle", graph), celex="32024R2847", short_name="cra")

    assert "toUpper(n.id) STARTS WITH $prefix" in graph.calls[0].query
    assert graph.calls[0].params == {"prefix": "CRA-", "celex": "32024R2847"}


def test_preflight_query_is_case_insensitive_so_a_legacy_lowercase_node_is_caught() -> None:
    """The legacy celex-less preflight matches ``toUpper(n.id)`` against an
    upper-cased id, so a legacy ``cra-1.0`` node is found when ``CRA-1.0`` is ingested.
    """
    graph = FakeGraphHandle([FakeQueryResult([["cra-1.0"]])])

    found = _is_already_merged(cast("GraphHandle", graph), "CRA-1.0")

    assert found is True
    assert "toUpper(n.id) = $id" in graph.calls[0].query
    assert graph.calls[0].params == {"id": "CRA-1.0"}


def test_normalize_short_name_uppercases() -> None:
    """The public normalizer upper-cases (and is idempotent)."""
    assert normalize_short_name("cra") == "CRA"
    assert normalize_short_name("Cra") == "CRA"
    assert normalize_short_name("CRA") == "CRA"


def test_resolve_ingestion_entry_normalizes_the_short_name_for_checks_and_entry(
    make_emitter: MakeEmitter,
) -> None:
    """``resolve_ingestion_entry`` normalizes once at the top; the
    collision query, the returned entry and (by construction) every downstream consumer
    see the upper-case short_name.
    """
    graph = FakeGraphHandle()
    emitter, _ = make_emitter()

    resolution = resolve_ingestion_entry(
        _CURATED_CELEX,
        "cra",
        single_tenant_graph=graph,
        emitter=emitter,
        cellar_fetch=_CountingFetch(default_cellar_xhtml(_CURATED_CELEX)),
        cellar_fetch_rdf=_CountingFetch(default_cellar_rdf(_CURATED_CELEX)),
    )

    assert resolution.entry.short_name == "CRA"
    assert graph.calls[-1].params == {"prefix": "CRA-", "celex": _CURATED_CELEX}


# --- resolve_via_cellar ------------------------------------------------------


def test_resolve_via_cellar_returns_catalog_entry_with_derived_short_name_and_base_version() -> (
    None
):
    """A happy-path Cellar resolution derives short_name/version/title."""
    fetch = _CountingFetch(_FIXTURE_REGULATION_A)
    fetch_rdf = _CountingFetch(_RDF_FIXTURE_REGULATION_A)

    resolution = resolve_via_cellar(
        _NONCURATED_CELEX, cellar_fetch=fetch, cellar_fetch_rdf=fetch_rdf
    )

    assert resolution.entry.celex == _NONCURATED_CELEX
    assert resolution.entry.title == "Regulation (EU) 1111/1111 Fixture A"
    assert resolution.entry.short_name == _derive_short_name(
        "Regulation (EU) 1111/1111 Fixture A", _NONCURATED_CELEX
    )
    assert resolution.entry.version == "1.0"


def test_resolve_via_cellar_uses_caller_supplied_short_name_verbatim_when_given() -> None:
    """When a caller supplies `short_name`
    explicitly (the MCP `ingest_regulation` path), it is used as given and
    `_derive_short_name` is never reached -- proven by passing a value that
    does not match what `_derive_short_name` would have derived from this
    fixture's own title.
    """
    fetch = _CountingFetch(_FIXTURE_REGULATION_A)
    fetch_rdf = _CountingFetch(_RDF_FIXTURE_REGULATION_A)

    resolution = resolve_via_cellar(
        _NONCURATED_CELEX,
        short_name="caller-supplied-name",
        cellar_fetch=fetch,
        cellar_fetch_rdf=fetch_rdf,
    )

    assert resolution.entry.short_name == "caller-supplied-name"
    assert resolution.entry.celex == _NONCURATED_CELEX
    assert resolution.entry.title == "Regulation (EU) 1111/1111 Fixture A"


def test_resolve_via_cellar_omitting_short_name_still_derives_exactly_as_before() -> None:
    """REST's call site
    (`routes.py:145`, `resolve_via_cellar(request_body.celex)`) never passes
    `short_name`, so this is REST's exact existing call shape -- confirming
    the new optional keyword-only parameter is fully backward compatible and
    the derivation behavior is completely unchanged when omitted.
    """
    fetch = _CountingFetch(_FIXTURE_REGULATION_A)
    fetch_rdf = _CountingFetch(_RDF_FIXTURE_REGULATION_A)

    resolution = resolve_via_cellar(
        _NONCURATED_CELEX, cellar_fetch=fetch, cellar_fetch_rdf=fetch_rdf
    )

    assert resolution.entry.short_name == _derive_short_name(
        "Regulation (EU) 1111/1111 Fixture A", _NONCURATED_CELEX
    )


def test_resolve_via_cellar_fetches_document_at_most_once_when_stage_one_reuses_adapter() -> None:
    """One HTTP fetch total per document (XHTML, RDF) across resolution +
    a simulated Stage 1 call -- the direct regression guard for the "fetch-once,
    replay both cached payloads" mechanism. Without the
    RDF-side replay, Stage 1 would issue a third real Cellar/ELI request for the RDF
    document, silently doubling the per-resolution request cost.
    """
    fetch = _CountingFetch(_FIXTURE_REGULATION_A)
    fetch_rdf = _CountingFetch(_RDF_FIXTURE_REGULATION_A)

    resolution = resolve_via_cellar(
        _NONCURATED_CELEX, cellar_fetch=fetch, cellar_fetch_rdf=fetch_rdf
    )
    resolution.adapter.fetch_regulatory_instrument_structure(_NONCURATED_CELEX)

    assert fetch.call_count == 1
    assert fetch_rdf.call_count == 1


def test_resolve_via_cellar_not_found_on_cellar_404_does_not_mark_unhealthy() -> None:
    """A genuine Cellar 404 is a not-found, not an outage."""

    def _not_found_fetch(celex: str) -> bytes:
        raise CellarNotFoundError(f"CELEX {celex!r} was not found on Cellar/ELI")

    with pytest.raises(CatalogIdentifierNotFoundError):
        resolve_via_cellar(_NONCURATED_CELEX, cellar_fetch=_not_found_fetch)

    assert is_healthy(CELLAR_ELI) is True


def test_resolve_via_cellar_raises_pipeline_stage_error_stage_ingestion_on_cellar_outage(
    configured_logging: Path,
) -> None:
    """A genuine Cellar outage during resolution surfaces as a 502-shaped stage error.

    `resolve_via_cellar` passes no explicit `emitter` to `_classify_stage_failure`
    (mirroring the route's own no-explicit-emitter call), so a
    non-whitelisted exception's server-side log line goes through the
    process-default facade -- `configured_logging` installs one for this test.
    """

    def _failing_fetch(celex: str) -> bytes:
        raise CellarFetchError(f"Cellar/ELI fetch failed for CELEX {celex!r}")

    with pytest.raises(PipelineStageError) as exc_info:
        resolve_via_cellar(_NONCURATED_CELEX, cellar_fetch=_failing_fetch)

    assert exc_info.value.stage == "ingestion"


def test_resolve_via_cellar_raises_pipeline_stage_error_on_cellar_parse_error(
    configured_logging: Path,
) -> None:
    """A CellarParseError from extract_metadata (no admissible RDF subject asserts
    the target predicate, `_RDF_FIXTURE_EMPTY`) surfaces as a 502-shaped stage
    error naming the ingestion stage.
    """
    fetch = _CountingFetch(_FIXTURE_UNRESOLVABLE_EFFECTIVE_DATE)
    fetch_rdf = _CountingFetch(_RDF_FIXTURE_EMPTY)

    with pytest.raises(PipelineStageError) as exc_info:
        resolve_via_cellar(_NONCURATED_CELEX, cellar_fetch=fetch, cellar_fetch_rdf=fetch_rdf)

    assert exc_info.value.stage == "ingestion"


def test_resolve_via_cellar_raises_pipeline_stage_error_on_empty_extracted_title(
    configured_logging: Path,
) -> None:
    """Flaw-review fix: an empty extracted title (no `eli-main-title` div) must not
    silently reach `_derive_short_name`.

    `extract_metadata` itself raises `pydantic.ValidationError` for an empty
    title (`RegulatoryInstrumentMetadata.title` is `Field(min_length=1)`) --
    not a silent `title == ""` return. `resolve_via_cellar`'s `except Exception`
    around `extract_metadata` (CellarParseError
    alone would miss it) still classifies it as stage `"ingestion"`, satisfying
    the same behavioral guarantee the flaw-review's empty-title fix intended:
    no degenerate `_<celex>`-shaped short_name is ever produced.
    """
    fetch = _CountingFetch(_FIXTURE_NO_TITLE)
    fetch_rdf = _CountingFetch(_RDF_FIXTURE_REGULATION_A)

    with pytest.raises(PipelineStageError) as exc_info:
        resolve_via_cellar(_NONCURATED_CELEX, cellar_fetch=fetch, cellar_fetch_rdf=fetch_rdf)

    assert exc_info.value.stage == "ingestion"


# --- run_status wiring -------------------------------------------------------


class _StageOrderIngest:
    """Fake ingest stage asserting `run_status.get_stage` already names it before running."""

    def __init__(self, run_id: str, *, rid: str = "CRA-1.0") -> None:
        """Prime the run id to check and the id this stage returns."""
        self._run_id = run_id
        self._rid = rid

    def __call__(
        self,
        identifier: str,
        short_name: str,
        *,
        version: str,
        adapter: object,
        graph: GraphHandle,
        submitter: object,
        audit_event_id: str,
        run_id: str | None = None,
        emitter: LogEmitter | None = None,
    ) -> IngestResult:
        """Assert `get_stage` already reads "ingestion", then return a canned result."""
        _ = (identifier, short_name, version, adapter, graph, submitter, audit_event_id, emitter)
        assert get_stage(self._run_id) == "ingestion"
        return IngestResult(
            regulatory_instrument_id=self._rid, run_id=run_id or "fake-run", counts={}
        )


class _StageOrderExtract:
    """Fake extract stage asserting `run_status.get_stage` already names it before running."""

    def __init__(self, run_id: str) -> None:
        """Prime the run id to check."""
        self._run_id = run_id

    def __call__(
        self,
        regulatory_instrument_id: str,
        *,
        adapter: object,
        native_graph: GraphHandle,
        submitter: GroupSubmitter,
        baseline_graph_name: str,
        audit_event_id: str,
        model: str,
        call_completion: CompletionCaller | None = None,
        emitter: LogEmitter | None = None,
    ) -> ExtractionResult:
        """Assert `get_stage` already reads "extraction", then return a canned result."""
        _ = (
            adapter,
            native_graph,
            model,
            call_completion,
            emitter,
            submitter,
            baseline_graph_name,
            audit_event_id,
        )
        assert get_stage(self._run_id) == "extraction"
        return ExtractionResult(
            regulatory_instrument_id=regulatory_instrument_id,
            role_node_ids={},
            requirement_ids=(),
            candidate_count=0,
            skipped_unit_count=0,
            requirement_id_collisions=(),
        )


class _StageOrderDerive:
    """Fake derive stage asserting `run_status.get_stage` already names it before running."""

    def __init__(self, run_id: str) -> None:
        """Prime the run id to check."""
        self._run_id = run_id

    def __call__(
        self,
        regulatory_instrument_id: str,
        *,
        baseline_graph: GraphHandle,
        submitter: GroupSubmitter,
        baseline_graph_name: str,
        audit_event_id: str,
        model: str,
        call_completion: CompletionCaller | None = None,
        emitter: LogEmitter | None = None,
    ) -> DerivationResult:
        """Assert `get_stage` already reads "derivation", then return a canned result."""
        _ = (
            baseline_graph,
            model,
            call_completion,
            emitter,
            submitter,
            baseline_graph_name,
            audit_event_id,
        )
        assert get_stage(self._run_id) == "derivation"
        return DerivationResult(
            regulatory_instrument_id=regulatory_instrument_id,
            obligation_node_ids=(),
            capability_node_ids=(),
            unmatched_requirement_ids=(),
            unmatched_obligation_ids=(),
        )


class _StageOrderMerge:
    """Fake merge stage asserting `run_status.get_stage` already names it before running."""

    def __init__(self, run_id: str) -> None:
        """Prime the run id to check."""
        self._run_id = run_id

    def __call__(
        self,
        regulatory_instrument_id: str,
        *,
        baseline_graph: GraphHandle,
        single_tenant_graph: GraphHandle,
        submitter: GroupSubmitter,
        single_tenant_graph_name: str,
        baseline_graph_name: str,
        audit_event_id: str,
        embed_model: str,
        merge_threshold: float | None,
        review_threshold: float | None,
        role_review_threshold: float | None,
        call_embedding: EmbeddingCaller | None = None,
        emitter: LogEmitter | None = None,
    ) -> MergeResult:
        """Assert `get_stage` already reads "merge", then return a canned result."""
        _ = (
            baseline_graph,
            single_tenant_graph,
            submitter,
            single_tenant_graph_name,
            baseline_graph_name,
            audit_event_id,
            embed_model,
            merge_threshold,
            review_threshold,
            call_embedding,
            emitter,
        )
        assert get_stage(self._run_id) == "merge"
        return MergeResult(
            regulatory_instrument_id=regulatory_instrument_id,
            obligation_ids=(),
            capability_canonical_ids=(),
            near_misses=(),
        )


def test_execute_catalog_stages_records_each_stage_as_current_before_running_it(
    make_emitter: MakeEmitter,
) -> None:
    """`set_stage(run_id, name)` happens before each `_run_stage(name, ...)`
    call, not after -- each fake stage asserts `get_stage(run_id)` already names itself
    from inside its own `__call__`, so a poller would see "currently executing", not
    "just completed".
    """
    emitter, _ = make_emitter()
    run_id = "run-status-order"
    fake = build_fake_pipeline_dependencies()
    dependencies = replace(
        fake.dependencies,
        stages=replace(
            fake.dependencies.stages,
            ingest=_StageOrderIngest(run_id),
            extract=_StageOrderExtract(run_id),
            derive=_StageOrderDerive(run_id),
            merge=_StageOrderMerge(run_id),
        ),
    )

    run_catalog_ingestion_pipeline(
        _ENTRY,
        config=_complete_config(),
        run_id=run_id,
        caller="127.0.0.1",
        dependencies=dependencies,
        emitter=emitter,
    )


def test_execute_catalog_stages_default_runs_all_four_in_order(
    make_emitter: MakeEmitter,
) -> None:
    """The shared sequence with no stage subset runs
    ingestion -> extraction -> derivation -> merge in order, feeds each later stage
    the id the ingest stage returned, and returns that id plus one report per stage
    in pipeline order.
    """
    emitter, _ = make_emitter()
    fake = build_fake_pipeline_dependencies(rid="CRA-9.9")

    rid, reports = _execute_catalog_stages(
        _ENTRY,
        run_id="run-characterize",
        resolved=_require_ingestion_config(_complete_config()),
        graphs=_OpenGraphs(
            native=fake.native, baseline=fake.baseline, single_tenant=fake.single_tenant
        ),
        dependencies=fake.dependencies,
        emitter=emitter,
        graph_writes=graph_write_access(_complete_config()),
    )

    expected_order = ["ingestion", "extraction", "derivation", "merge"]
    assert fake.recorder.order == expected_order
    assert [call.regulatory_instrument_id for call in fake.recorder.calls[1:]] == ["CRA-9.9"] * 3
    assert rid == "CRA-9.9"
    assert [report.stage for report in reports] == [
        "ingestion",
        "extraction",
        "derivation",
        "merge",
    ]


def _run_selected(
    fake: FakePipeline,
    emitter: LogEmitter,
    *,
    stages_to_run: tuple[str, ...] | None,
    completed: list[str] | None = None,
) -> tuple[str, tuple[StageReport, ...]]:
    """Drive `_execute_catalog_stages` over `fake`, optionally recording `on_stage_complete`."""
    return _execute_catalog_stages(
        _ENTRY,
        run_id="run-subset",
        resolved=_require_ingestion_config(_complete_config()),
        graphs=_OpenGraphs(
            native=fake.native, baseline=fake.baseline, single_tenant=fake.single_tenant
        ),
        dependencies=fake.dependencies,
        emitter=emitter,
        stages_to_run=stages_to_run,
        on_stage_complete=completed.append if completed is not None else None,
        graph_writes=graph_write_access(_complete_config()),
    )


def test_execute_catalog_stages_subset_skips_unrequested_stages_and_derives_rid(
    make_emitter: MakeEmitter,
) -> None:
    """With `ingestion` not requested the ingest stage never runs and the
    instrument id is `<short_name>-<version>` (the formula the ingest stage uses), so
    the later stages still receive an id; only the requested stages are reported.
    """
    emitter, _ = make_emitter()
    fake = build_fake_pipeline_dependencies(rid="SHOULD-NOT-BE-USED")

    rid, reports = _run_selected(fake, emitter, stages_to_run=("derivation", "merge"))

    assert fake.recorder.order == ["derivation", "merge"]
    assert [call.regulatory_instrument_id for call in fake.recorder.calls] == ["CRA-1.0"] * 2
    assert rid == "CRA-1.0"
    assert [report.stage for report in reports] == ["derivation", "merge"]


def test_stage_order_mirrors_the_change_monitor_pipeline_stages() -> None:
    """`api` cannot import `change_monitor` at module level, so the two
    stage-name tuples are duplicated and must never drift.
    """
    assert _STAGE_ORDER == PIPELINE_STAGES


def test_execute_catalog_stages_reports_each_completed_stage_to_the_callback_in_order(
    make_emitter: MakeEmitter,
) -> None:
    """`on_stage_complete(name)` fires once per stage, after that stage returned."""
    emitter, _ = make_emitter()
    completed: list[str] = []

    _run_selected(
        build_fake_pipeline_dependencies(), emitter, stages_to_run=None, completed=completed
    )

    assert completed == ["ingestion", "extraction", "derivation", "merge"]


def test_execute_catalog_stages_does_not_report_a_stage_that_failed(
    make_emitter: MakeEmitter,
) -> None:
    """A failing stage is never reported complete, and later stages never run."""
    emitter, _ = make_emitter()
    completed: list[str] = []
    fake = build_fake_pipeline_dependencies(derive_error=DomainMapperExtractionError("boom"))

    with pytest.raises(PipelineStageError):
        _run_selected(fake, emitter, stages_to_run=None, completed=completed)

    assert completed == ["ingestion", "extraction"]
    assert fake.recorder.order == ["ingestion", "extraction", "derivation"]


def test_run_catalog_ingestion_pipeline_clears_stage_tracking_on_success(
    make_emitter: MakeEmitter,
) -> None:
    """Once a run succeeds, its `run_status` entry is cleared."""
    emitter, _ = make_emitter()
    run_id = "run-status-success"
    fake = build_fake_pipeline_dependencies()

    run_catalog_ingestion_pipeline(
        _ENTRY,
        config=_complete_config(),
        run_id=run_id,
        caller="127.0.0.1",
        dependencies=fake.dependencies,
        emitter=emitter,
    )

    assert get_stage(run_id) is None


def test_run_catalog_ingestion_pipeline_clears_stage_tracking_on_stage_failure(
    make_emitter: MakeEmitter,
) -> None:
    """A run that aborts on a stage failure still clears its `run_status` entry."""
    emitter, _ = make_emitter()
    run_id = "run-status-failure"
    fake = build_fake_pipeline_dependencies(
        extract_error=DomainMapperExtractionError("unit 7 produced no candidates")
    )

    with pytest.raises(PipelineStageError):
        run_catalog_ingestion_pipeline(
            _ENTRY,
            config=_complete_config(),
            run_id=run_id,
            caller="127.0.0.1",
            dependencies=fake.dependencies,
            emitter=emitter,
        )

    assert get_stage(run_id) is None


def test_merge_summary_includes_pending_reviews_key() -> None:
    """`_merge_summary`'s dict carries a `pending_reviews` key sourced from
    `MergeResult.pending_review_count`, alongside the `near_misses` key
    (additive, not a rename/replace;
    `ps-cli`'s own `_parse_stage_outcome` already iterates `summary.items()`
    generically, so an extra key never breaks it).
    """
    result = MergeResult(
        regulatory_instrument_id="ri-summary",
        obligation_ids=("obl-1",),
        capability_canonical_ids=("cap-1", "cap-2"),
        near_misses=(
            NearMissPair(
                incoming_id="cap-incoming",
                incoming_text="Incoming Capability",
                nearest_existing_id="cap-existing",
                nearest_existing_text="Existing Capability",
                similarity=0.5,
            ),
        ),
        pending_review_count=1,
    )

    summary = _merge_summary(result)

    assert summary["near_misses"] == 1
    assert summary["pending_reviews"] == 1


def test_merge_summary_pending_reviews_zero_when_no_near_misses() -> None:
    """The default/no-near-miss case: `pending_reviews` is present and 0 --
    the key is always emitted (not conditionally omitted), matching
    `near_misses`'s own always-present convention. `handlers.py`'s CLI-side
    conditional-append is what keeps the *terminal* line unchanged, not this
    dict, which stays a plain summary of counts.
    """
    result = MergeResult(
        regulatory_instrument_id="ri-summary-2",
        obligation_ids=(),
        capability_canonical_ids=("cap-1",),
        near_misses=(),
    )

    summary = _merge_summary(result)

    assert summary["near_misses"] == 0
    assert summary["pending_reviews"] == 0


def test_merge_stage_summary_exposes_new_and_matched_counts() -> None:
    """The merge stage summary carries `new_obligations`, `new_capabilities`
    and `matched_capabilities` (`StageOutcome.summary` is `dict[str, int]`), sourced
    from the `MergeResult` fields, alongside the other keys.
    """
    result = MergeResult(
        regulatory_instrument_id="ri-summary-3",
        obligation_ids=("obl-1", "obl-2", "obl-3"),
        capability_canonical_ids=("cap-1", "cap-2", "cap-3"),
        near_misses=(),
        new_capability_count=1,
        matched_capability_count=2,
        new_obligation_count=3,
    )

    summary = _merge_summary(result)

    assert summary["new_obligations"] == 3
    assert summary["new_capabilities"] == 1
    assert summary["matched_capabilities"] == 2
    assert summary["obligations"] == 3
    assert summary["canonical_capabilities"] == 3


def test_merge_stage_summary_counts_default_to_zero() -> None:
    result = MergeResult(
        regulatory_instrument_id="ri-summary-4",
        obligation_ids=(),
        capability_canonical_ids=(),
        near_misses=(),
    )

    summary = _merge_summary(result)

    assert (summary["new_obligations"], summary["new_capabilities"]) == (0, 0)
    assert summary["matched_capabilities"] == 0


def test_merge_summary_reports_the_role_near_misses_raised() -> None:
    result = MergeResult(
        regulatory_instrument_id="ri-summary-5",
        obligation_ids=(),
        capability_canonical_ids=(),
        near_misses=(),
        pending_review_count=3,
        role_near_miss_count=2,
    )

    summary = _merge_summary(result)

    assert summary["role_near_misses"] == 2
    assert summary["pending_reviews"] == 3


def test_merge_summary_role_near_misses_default_to_zero() -> None:
    result = MergeResult(
        regulatory_instrument_id="ri-summary-6",
        obligation_ids=(),
        capability_canonical_ids=(),
        near_misses=(),
    )

    assert _merge_summary(result)["role_near_misses"] == 0


def test_extraction_summary_reports_roles_minted_and_non_actor_candidates_dropped() -> None:
    result = ExtractionResult(
        regulatory_instrument_id="ri-summary-7",
        role_node_ids={"Manufacturer": "role_m", "manufacturers": "role_m", "Importer": "role_i"},
        requirement_ids=("req_1",),
        candidate_count=1,
        skipped_unit_count=0,
        requirement_id_collisions=(),
        roles_minted=2,
        non_actor_roles_dropped=3,
    )

    summary = _extraction_summary(result)

    assert summary["roles_minted"] == 2
    assert summary["non_actor_roles_dropped"] == 3


def test_extraction_summary_counts_a_role_once_however_many_spellings_map_to_it() -> None:
    result = ExtractionResult(
        regulatory_instrument_id="ri-summary-7b",
        role_node_ids={"Manufacturer": "role_m", "manufacturers": "role_m", "Importer": "role_i"},
        requirement_ids=("req_1",),
        candidate_count=3,
        skipped_unit_count=0,
        requirement_id_collisions=(),
        roles_minted=2,
    )

    assert _extraction_summary(result)["roles"] == 2


def test_extraction_summary_keeps_the_roles_key_and_defaults_the_new_counts_to_zero() -> None:
    result = ExtractionResult(
        regulatory_instrument_id="ri-summary-8",
        role_node_ids={},
        requirement_ids=(),
        candidate_count=0,
        skipped_unit_count=0,
        requirement_id_collisions=(),
    )

    summary = _extraction_summary(result)

    assert (summary["roles"], summary["roles_minted"], summary["non_actor_roles_dropped"]) == (
        0,
        0,
        0,
    )


def test_internal_ingestion_summary_reports_roles_minted_and_zero_non_actor_drops() -> None:
    result = InternalIngestResult(
        regulatory_instrument_id="ENGPRAC-1.0",
        role_count=4,
        requirement_count=0,
        obligation_count=0,
        capability_count=0,
        policy_count=0,
        standard_count=0,
        control_count=0,
        practice_area_count=0,
        risk_path_count=0,
    )

    summary = _internal_ingestion_summary(result)

    assert summary["roles"] == 4
    assert summary["roles_minted"] == 4
    assert summary["non_actor_roles_dropped"] == 0


# --- reject redundant re-ingestion of an already-merged instrument -----------------


def test_preflight_check_queries_single_tenant_graph_before_any_stage(
    make_emitter: MakeEmitter,
) -> None:
    """The pre-flight check runs, querying the single-tenant graph
    for the resolved ``{short_name}-{version}`` id, before any stage.
    """
    emitter, _ = make_emitter()
    fake = build_fake_pipeline_dependencies(rid="CRA-1.0")

    run_catalog_ingestion_pipeline(
        _ENTRY,
        config=_complete_config(),
        run_id="run-preflight-miss",
        caller="127.0.0.1",
        dependencies=fake.dependencies,
        emitter=emitter,
    )

    assert len(fake.single_tenant.calls) == 1
    assert fake.single_tenant.calls[0].params == {"id": f"{_ENTRY.short_name}-{_ENTRY.version}"}


def test_preflight_miss_runs_pipeline_exactly_as_before(make_emitter: MakeEmitter) -> None:
    """When no fully-merged instrument exists, every stage still runs
    and the outcome is reported as ``"fresh"``.
    """
    emitter, _ = make_emitter()
    fake = build_fake_pipeline_dependencies(rid="CRA-1.0")

    outcome = run_catalog_ingestion_pipeline(
        _ENTRY,
        config=_complete_config(),
        run_id="run-preflight-miss-2",
        caller="127.0.0.1",
        dependencies=fake.dependencies,
        emitter=emitter,
    )

    assert outcome.outcome == "fresh"
    expected_order = ["ingestion", "extraction", "derivation", "merge"]
    assert fake.recorder.order == expected_order
    assert [report.stage for report in outcome.stages] == [
        "ingestion",
        "extraction",
        "derivation",
        "merge",
    ]


def test_preflight_hit_skips_domain_mapper_and_company_merge(make_emitter: MakeEmitter) -> None:
    """An existing fully-merged instrument skips every stage and
    the response reports the distinct ``"already_ingested"`` outcome with no
    stage reports at all.
    """
    emitter, _ = make_emitter()
    fake = build_fake_pipeline_dependencies(preflight_hit=True)

    outcome = run_catalog_ingestion_pipeline(
        _ENTRY,
        config=_complete_config(),
        run_id="run-preflight-hit",
        caller="127.0.0.1",
        dependencies=fake.dependencies,
        emitter=emitter,
    )

    assert outcome.outcome == "already_ingested"
    assert outcome.regulatory_instrument_id == f"{_ENTRY.short_name}-{_ENTRY.version}"
    assert outcome.stages == ()
    assert fake.recorder.order == []


def test_preflight_check_failure_fails_closed_as_pipeline_stage_error(
    make_emitter: MakeEmitter,
) -> None:
    """A pre-flight query failure (e.g. graph unreachable) surfaces as
    a ``PipelineStageError`` naming the ``"preflight"`` stage -- never silently
    treated as already ingested, and no stage ever runs.
    """
    emitter, _ = make_emitter()
    fake = build_fake_pipeline_dependencies(
        preflight_error=FalkorDBConnectionError("FalkorDB connection failed at 10.0.0.5:6379")
    )

    with pytest.raises(PipelineStageError) as exc_info:
        run_catalog_ingestion_pipeline(
            _ENTRY,
            config=_complete_config(),
            run_id="run-preflight-error",
            caller="127.0.0.1",
            dependencies=fake.dependencies,
            emitter=emitter,
        )

    assert exc_info.value.stage == "preflight"
    assert exc_info.value.reason == "preflight failed"
    assert fake.recorder.order == []


# --- check_short_name_collision ----------------------------------------------


def test_check_short_name_collision_returns_the_conflicting_celex_when_a_different_celex_holds_the_prefix() -> (  # noqa: E501 - long descriptive test name
    None
):
    """A different CELEX already ingested under this exact ``short_name`` is a real collision."""
    graph = FakeGraphHandle([FakeQueryResult([["cra-1.0", "32024R0001"]])])

    conflict = check_short_name_collision(
        cast("GraphHandle", graph), celex="32024R2847", short_name="cra"
    )

    assert conflict == "32024R0001"


def test_check_short_name_collision_returns_none_when_no_row_matches() -> None:
    """No candidate row at all means no collision."""
    graph = FakeGraphHandle([FakeQueryResult([])])

    conflict = check_short_name_collision(
        cast("GraphHandle", graph), celex="32024R2847", short_name="cra"
    )

    assert conflict is None


def test_check_short_name_collision_returns_none_when_the_only_matching_row_is_the_same_celex() -> (
    None
):
    """The ``n.celex <> $celex`` clause excludes self-matches -- load-bearing for idempotency.

    The fake graph here stands in for what the real Cypher's ``WHERE ... AND n.celex <>
    $celex`` clause would already have filtered out server-side; this proves
    ``check_short_name_collision`` doesn't independently re-introduce a self-match some
    other way.
    """
    graph = FakeGraphHandle([FakeQueryResult([])])

    conflict = check_short_name_collision(
        cast("GraphHandle", graph), celex="32024R2847", short_name="cra"
    )

    assert conflict is None


def test_check_short_name_collision_returns_none_when_a_candidate_row_only_shares_a_hyphenated_prefix() -> (  # noqa: E501 - long descriptive test name
    None
):
    """A ``STARTS WITH`` hit isn't a real collision unless the
    candidate's own ``rpartition("-")``-derived short name is an exact match.

    ``"cra-legacy-1.0"``'s recorded short name is ``"cra-legacy"`` (rpartition splits off
    only the trailing ``"1.0"`` version segment) -- not ``"cra"`` -- so this must not be
    reported as a collision even though ``"cra-legacy-1.0"`` starts with the ``"cra-"``
    prefix used to prefilter candidates DB-side.
    """
    graph = FakeGraphHandle([FakeQueryResult([["cra-legacy-1.0", "32024R0001"]])])

    conflict = check_short_name_collision(
        cast("GraphHandle", graph), celex="32024R2847", short_name="cra"
    )

    assert conflict is None
