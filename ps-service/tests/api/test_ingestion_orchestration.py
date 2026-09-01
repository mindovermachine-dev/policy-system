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

from dataclasses import replace
from typing import TYPE_CHECKING, cast

import pytest

from api._fakes import (
    FakeIngestionAdapter,
    MakeEmitter,
    ReadLines,
    build_fake_pipeline_dependencies,
)
from ps_service.api.catalog import CatalogEntry
from ps_service.api.errors import (
    CatalogIdentifierNotFoundError,
    IngestionConfigIncompleteError,
    PipelineStageError,
)
from ps_service.api.ingestion_orchestration import (
    PipelineStages,
    _classify_stage_failure,  # pyright: ignore[reportPrivateUsage] — internal helper under test
    _derive_short_name,  # pyright: ignore[reportPrivateUsage] — internal helper under test
    resolve_via_cellar,
    run_catalog_ingestion_pipeline,
)
from ps_service.api.run_status import get_stage
from ps_service.company_merge.models import MergeResult
from ps_service.config import ServiceConfig
from ps_service.dependency_health import CELLAR_ELI, is_healthy
from ps_service.domain_mapper.errors import DomainMapperExtractionError
from ps_service.domain_mapper.models import DerivationResult, ExtractionResult
from ps_service.ingestion.adapters.errors import CellarFetchError, CellarNotFoundError
from ps_service.ingestion.falkordb_client import FalkorDBConnectionError
from ps_service.ingestion.models import IngestResult

if TYPE_CHECKING:
    from pathlib import Path

    from ps_service.api.ingestion_orchestration import GraphHandle
    from ps_service.ingestion.adapters.base import IngestionAdapter
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


def test_derive_short_name_slugifies_title_words_and_appends_lowercase_celex() -> None:
    """AC-BI-004: the slug is the title's words, lowercased and `_`-joined, with the
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


def test_derivation_stage_summary_reports_unmatched_obligations_count(
    make_emitter: MakeEmitter,
) -> None:
    """Issue #64 slice 9: the derivation stage's ``StageReport.summary`` dict
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


# --- injected ingestion_adapter (AC-BI-006 wiring) ---------------------------


def test_run_catalog_ingestion_pipeline_uses_injected_ingestion_adapter_when_provided(
    make_emitter: MakeEmitter,
) -> None:
    """AC-BI-006: a caller-supplied ``ingestion_adapter`` is passed to the ingest stage
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

    Shortened from PLAN.md's literal
    `..._falls_back_to_default_adapter_factory_when_ingestion_adapter_omitted` -- 9
    chars over the 100-col limit; same assertion, same intent.
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


# --- resolve_via_cellar (AC-BI-003/004/005/006/007) --------------------------


def test_resolve_via_cellar_returns_catalog_entry_with_derived_short_name_and_base_version() -> (
    None
):
    """AC-BI-004: a happy-path Cellar resolution derives short_name/version/title."""
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


def test_resolve_via_cellar_fetches_document_at_most_once_when_stage_one_reuses_adapter() -> None:
    """AC-BI-006/D2: one HTTP fetch total per document (XHTML, RDF) across resolution +
    a simulated Stage 1 call -- the direct regression guard for the "fetch-once,
    replay both cached payloads" mechanism (PLAN_REVISED.md §6 item 2). Without the
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
    """AC-BI-005/007: a genuine Cellar 404 is a not-found, not an outage."""

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
    (mirroring the route's own no-explicit-emitter call, Increment 8), so a
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
    around `extract_metadata` (added for exactly this reason -- CellarParseError
    alone would miss it) still classifies it as stage `"ingestion"`, satisfying
    the same behavioral guarantee the flaw-review's empty-title fix intended:
    no degenerate `_<celex>`-shaped short_name is ever produced.
    """
    fetch = _CountingFetch(_FIXTURE_NO_TITLE)
    fetch_rdf = _CountingFetch(_RDF_FIXTURE_REGULATION_A)

    with pytest.raises(PipelineStageError) as exc_info:
        resolve_via_cellar(_NONCURATED_CELEX, cellar_fetch=fetch, cellar_fetch_rdf=fetch_rdf)

    assert exc_info.value.stage == "ingestion"


# --- run_status wiring (AC-BI-008, Increment 11) -----------------------------


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
        run_id: str | None = None,
        emitter: LogEmitter | None = None,
    ) -> IngestResult:
        """Assert `get_stage` already reads "ingestion", then return a canned result."""
        _ = (identifier, short_name, version, adapter, graph, emitter)
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
        baseline_graph: GraphHandle,
        model: str,
        call_completion: CompletionCaller | None = None,
        emitter: LogEmitter | None = None,
    ) -> ExtractionResult:
        """Assert `get_stage` already reads "extraction", then return a canned result."""
        _ = (adapter, native_graph, baseline_graph, model, call_completion, emitter)
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
        model: str,
        call_completion: CompletionCaller | None = None,
        emitter: LogEmitter | None = None,
    ) -> DerivationResult:
        """Assert `get_stage` already reads "derivation", then return a canned result."""
        _ = (baseline_graph, model, call_completion, emitter)
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
        embed_model: str,
        similarity_threshold: float | None,
        call_embedding: EmbeddingCaller | None = None,
        emitter: LogEmitter | None = None,
    ) -> MergeResult:
        """Assert `get_stage` already reads "merge", then return a canned result."""
        _ = (
            baseline_graph,
            single_tenant_graph,
            embed_model,
            similarity_threshold,
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
    """AC-BI-008: `set_stage(run_id, name)` happens before each `_run_stage(name, ...)`
    call, not after -- each fake stage asserts `get_stage(run_id)` already names itself
    from inside its own `__call__`, so a poller would see "currently executing", not
    "just completed".
    """
    emitter, _ = make_emitter()
    run_id = "run-status-order"
    fake = build_fake_pipeline_dependencies()
    dependencies = replace(
        fake.dependencies,
        stages=PipelineStages(
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


def test_run_catalog_ingestion_pipeline_clears_stage_tracking_on_success(
    make_emitter: MakeEmitter,
) -> None:
    """AC-BI-008: once a run succeeds, its `run_status` entry is cleared."""
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
    """AC-BI-008: a run that aborts on a stage failure still clears its `run_status` entry."""
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
