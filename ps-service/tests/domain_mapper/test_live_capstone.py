"""Increment 19 — the live 3-regulation end-to-end capstone
(PLAN_REVIEWED.md §11 Batch 11).

`@pytest.mark.falkordb_live @pytest.mark.llm_live`: reads #14's own
live-populated `cra_native`/`gdpr_native`/`nis2_native` graphs through the
real Cellar/ELI Domain Mapping Adapter, runs `ExtractRolesAndRequirements`
then `DeriveObligationsAndCapabilities` for each regulation against real
Azure OpenAI (via `route_completion`'s default caller), and writes to the
real, permanent `cra_baseline`/`gdpr_baseline`/`nis2_baseline` graphs —
the walking skeleton's actual output, left in place afterward (unlike
`test_baseline_graph_isolation.py`'s `_isolation_test`-suffixed throwaway
graphs, which ARE cleaned up).

**Bounded, not exhaustive** (Open Question 2's mitigation): each
regulation's extraction is capped to the first `_LIMIT_PER_REGULATORY_INSTRUMENT`
`ExtractionUnit`s (document order) via `_LimitedDomainMappingAdapter`, a
thin wrapper written here rather than a production-code change —
`extract_roles_and_requirements` only ever calls
`adapter.read_native_units(native_graph)` once and consumes the whole
result, so slicing at the adapter boundary is equivalent to, and simpler
than, plumbing a new `limit` parameter through the action's own signature.

Verifies AC-001 through AC-008 against real FalkorDB state via direct
Cypher reads — not just the two actions' in-memory return values.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

import pytest

from ps_service.config import load_config
from ps_service.domain_mapper.adapters.cellar_eli import CellarEliDomainMappingAdapter
from ps_service.domain_mapper.derivation import derive_obligations_and_capabilities
from ps_service.domain_mapper.extraction import extract_roles_and_requirements
from ps_service.domain_mapper.falkordb_client import (
    GraphHandle,
    baseline_graph_name,
    connect_from_config,
    native_graph_name,
    select_graph,
)
from ps_service.domain_mapper.models import ExtractionUnit
from ps_service.logging import LogEmitter, bind_run_context

if TYPE_CHECKING:
    from falkordb import FalkorDB

    from domain_mapper._fakes import MakeEmitter, ReadLines
    from ps_service.domain_mapper.adapters.base import DomainMappingAdapter
    from ps_service.domain_mapper.models import DerivationResult

_LIMIT_PER_REGULATORY_INSTRUMENT = 15
_LOG_FILENAME = "capstone.jsonl"
_GOVERNANCE_LABELS = ("Policy", "Standard", "Control")  # AC-008

# Captured at module-import time (collection), before tests/conftest.py's autouse
# `_isolate_logging` fixture runs `monkeypatch.delenv("PS_LLMINTERFACE_MODEL", ...)`
# for every test (that guard exists to keep a leaked `.env` value out of unrelated
# tests) — mirrors `test_route_completion_live_provider.py`'s established pattern
# exactly, for the same reason: this live test's whole point is to use the real
# configured model, so it must be read before that fixture strips it.
_LLM_INTERFACE_MODEL = os.environ.get("PS_LLMINTERFACE_MODEL")


@dataclass(frozen=True)
class _RegulatoryInstrumentFixture:
    short_name: str
    regulatory_instrument_id: str


_REGULATIONS = (
    _RegulatoryInstrumentFixture("CRA", "CRA-1.0"),
    _RegulatoryInstrumentFixture("GDPR", "GDPR-1.0"),
    _RegulatoryInstrumentFixture("NIS2", "NIS2-1.0"),
)


class _LimitedDomainMappingAdapter:
    """Wraps a real `DomainMappingAdapter`, capping the `ExtractionUnit`s
    returned to the first `limit` (the inner adapter's own document order).
    Satisfies `DomainMappingAdapter` structurally — no production code
    change needed for this test's bounding requirement.
    """

    def __init__(self, inner: DomainMappingAdapter, limit: int) -> None:
        self._inner = inner
        self._limit = limit

    def read_native_units(self, graph: GraphHandle) -> tuple[ExtractionUnit, ...]:
        return self._inner.read_native_units(graph)[: self._limit]


class _AnnexOnlyDomainMappingAdapter:
    """Wraps a real `DomainMappingAdapter`, filtering the `ExtractionUnit`s
    returned down to only annex-derived units (`citation_ref` starting with
    `"Annex "`). Satisfies `DomainMappingAdapter` structurally — no
    production code change needed for this test's bounding requirement.
    """

    def __init__(self, inner: DomainMappingAdapter) -> None:
        self._inner = inner

    def read_native_units(self, graph: GraphHandle) -> tuple[ExtractionUnit, ...]:
        return tuple(
            unit
            for unit in self._inner.read_native_units(graph)
            if unit.citation_ref.startswith("Annex ")
        )


class _FakeDomainMappingAdapter:
    """Test double for `DomainMappingAdapter`: returns a fixed tuple of
    `ExtractionUnit`s regardless of `graph`, for exercising
    `_AnnexOnlyDomainMappingAdapter`'s filter logic without FalkorDB.
    """

    def __init__(self, units: tuple[ExtractionUnit, ...]) -> None:
        self._units = units

    def read_native_units(self, graph: GraphHandle) -> tuple[ExtractionUnit, ...]:
        return self._units


def test_annex_only_domain_mapping_adapter_filters_to_annex_citation_refs_only() -> None:
    article_unit = ExtractionUnit(
        citation_ref="Art. 1",
        text="Subject matter.",
        article_number="1",
        paragraph_number="1",
        article_heading="Subject matter",
    )
    annex_i_unit = ExtractionUnit(
        citation_ref="Annex I",
        text="Essential cybersecurity requirements.",
        article_number="I",
        paragraph_number="1",
        article_heading="",
    )
    annex_ii_unit = ExtractionUnit(
        citation_ref="Annex II",
        text="Information and instructions for the user.",
        article_number="II",
        paragraph_number="1",
        article_heading="",
    )
    inner = _FakeDomainMappingAdapter((article_unit, annex_i_unit, annex_ii_unit))
    adapter = _AnnexOnlyDomainMappingAdapter(inner)

    result = adapter.read_native_units(cast("GraphHandle", object()))

    assert result == (annex_i_unit, annex_ii_unit)


@dataclass
class _RegulatoryInstrumentOutcome:
    native_citation_refs: set[str]
    derivation_result: DerivationResult
    extraction_run_id: str
    derivation_run_id: str


def _query_rows(
    graph: GraphHandle, query: str, params: dict[str, object] | None = None
) -> list[list[object]]:
    return cast("list[list[object]]", graph.query(query, params=params).result_set)


def _native_citation_refs(native_graph: GraphHandle) -> set[str]:
    article_refs = _query_rows(native_graph, "MATCH (a:ARTICLE) RETURN a.citation_ref")
    paragraph_refs = _query_rows(native_graph, "MATCH (p:PARAGRAPH) RETURN p.citation_ref")
    return {cast("str", row[0]) for row in article_refs} | {
        cast("str", row[0]) for row in paragraph_refs
    }


def _run_pipeline_for_regulatory_instrument(
    db: FalkorDB,
    adapter: DomainMappingAdapter,
    fixture: _RegulatoryInstrumentFixture,
    *,
    model: str,
    emitter: LogEmitter,
) -> _RegulatoryInstrumentOutcome:
    """Runs both actions for one regulation, each inside its own bound run
    context. A different `run_id` is bound for extraction vs. derivation —
    no orchestrator wires a single shared `run_id` across both actions yet
    (Open Question 6), so this test binds one per action per regulation
    (6 distinct run_ids total) to exercise AC-006/AC-007 unambiguously
    rather than leave the "same or different" choice implicit.
    """
    native_graph = select_graph(db, native_graph_name(fixture.short_name))
    baseline_graph = select_graph(db, baseline_graph_name(fixture.short_name))
    limited_adapter = _LimitedDomainMappingAdapter(adapter, _LIMIT_PER_REGULATORY_INSTRUMENT)
    native_citation_refs = _native_citation_refs(native_graph)

    extraction_run_id = f"capstone-{fixture.short_name.lower()}-extraction"
    with bind_run_context(extraction_run_id):
        extract_roles_and_requirements(
            fixture.regulatory_instrument_id,
            adapter=limited_adapter,
            native_graph=native_graph,
            baseline_graph=baseline_graph,
            model=model,
            emitter=emitter,
        )

    derivation_run_id = f"capstone-{fixture.short_name.lower()}-derivation"
    with bind_run_context(derivation_run_id):
        derivation_result = derive_obligations_and_capabilities(
            fixture.regulatory_instrument_id,
            baseline_graph=baseline_graph,
            model=model,
            emitter=emitter,
        )

    return _RegulatoryInstrumentOutcome(
        native_citation_refs=native_citation_refs,
        derivation_result=derivation_result,
        extraction_run_id=extraction_run_id,
        derivation_run_id=derivation_run_id,
    )


def _assert_ac001_provenance(
    baseline_graph: GraphHandle, native_refs: set[str], regulatory_instrument_id: str
) -> None:
    defines_refs = [
        row[0]
        for row in _query_rows(
            baseline_graph, "MATCH (:RegulatoryInstrument)-[e:DEFINES]->(:Role) RETURN e.source_ref"
        )
    ]
    expresses_refs = [
        row[0]
        for row in _query_rows(
            baseline_graph,
            "MATCH (:RegulatoryInstrument)-[e:EXPRESSES]->(:Requirement) RETURN e.source_ref",
        )
    ]
    assert defines_refs, f"{regulatory_instrument_id}: no DEFINES edges written"
    assert expresses_refs, f"{regulatory_instrument_id}: no EXPRESSES edges written"
    for ref in (*defines_refs, *expresses_refs):
        assert ref in native_refs, (
            f"{regulatory_instrument_id}: source_ref {ref!r} does not match any "
            f"native-graph element"
        )


def _assert_ac002_confidence(baseline_graph: GraphHandle, regulatory_instrument_id: str) -> None:
    role_confidences = [
        row[0] for row in _query_rows(baseline_graph, "MATCH (n:Role) RETURN n.confidence")
    ]
    requirement_confidences = [
        row[0] for row in _query_rows(baseline_graph, "MATCH (n:Requirement) RETURN n.confidence")
    ]
    all_confidences = role_confidences + requirement_confidences
    assert all_confidences, (
        f"{regulatory_instrument_id}: no confidence-bearing Role/Requirement nodes found"
    )
    for value in all_confidences:
        confidence = cast("float", value)
        assert 0.0 <= confidence <= 1.0, (
            f"{regulatory_instrument_id}: confidence out of range: {confidence!r}"
        )
    # Low-confidence existence is explicitly non-blocking (Open Question 8) —
    # deliberately no assertion either way on whether one happens to appear.


def _assert_ac003_derivation_shape(
    baseline_graph: GraphHandle,
    unmatched_ids: tuple[str, ...],
    unmatched_obligation_ids: tuple[str, ...],
    regulatory_instrument_id: str,
) -> None:
    requirement_rows = _query_rows(
        baseline_graph,
        "MATCH (req:Requirement) OPTIONAL MATCH (req)-[s:SATISFIED_BY]->(:Obligation) "
        "RETURN req.id, count(s)",
    )
    for requirement_id_value, satisfied_count in requirement_rows:
        if requirement_id_value in unmatched_ids:
            continue
        assert cast("int", satisfied_count) >= 1, (
            f"{regulatory_instrument_id}: Requirement {requirement_id_value!r} "
            f"has no SATISFIED_BY edge"
        )

    has_rows = _query_rows(
        baseline_graph,
        "MATCH (o:Obligation) OPTIONAL MATCH (:Role)-[h:HAS]->(o) RETURN o.id, count(h)",
    )
    for obligation_id_value, has_count in has_rows:
        assert cast("int", has_count) == 1, (
            f"{regulatory_instrument_id}: Obligation {obligation_id_value!r} has {has_count} HAS "
            "edges, expected exactly 1"
        )

    requires_rows = _query_rows(
        baseline_graph,
        "MATCH (o:Obligation) OPTIONAL MATCH (o)-[r:REQUIRES]->(:Capability) RETURN o.id, count(r)",
    )
    for obligation_id_value, requires_count in requires_rows:
        if obligation_id_value in unmatched_obligation_ids:
            continue
        assert cast("int", requires_count) >= 1, (
            f"{regulatory_instrument_id}: Obligation {obligation_id_value!r} has no REQUIRES edge"
        )


def _assert_ac005_regulatory_instrument_scope(
    baseline_graph: GraphHandle, regulatory_instrument_id: str
) -> None:
    regulatory_instrument_rows = _query_rows(
        baseline_graph, "MATCH (r:RegulatoryInstrument) RETURN r.id"
    )
    assert regulatory_instrument_rows == [[regulatory_instrument_id]], (
        f"unexpected Regulation node set in {regulatory_instrument_id}'s baseline graph: "
        f"{regulatory_instrument_rows}"
    )

    requirement_ids = [
        row[0] for row in _query_rows(baseline_graph, "MATCH (n:Requirement) RETURN n.id")
    ]
    prefix = f"{regulatory_instrument_id}_req_art_"
    for requirement_id_value in requirement_ids:
        assert cast("str", requirement_id_value).startswith(prefix), (
            f"Requirement {requirement_id_value!r} in {regulatory_instrument_id}'s "
            "baseline graph does not carry that regulation's own id prefix — "
            "possible cross-regulation contamination"
        )


def _assert_ac008_no_governance_nodes(
    baseline_graph: GraphHandle, regulatory_instrument_id: str
) -> None:
    for label in _GOVERNANCE_LABELS:
        count = _query_rows(baseline_graph, f"MATCH (n:{label}) RETURN count(n)")[0][0]
        assert count == 0, f"{regulatory_instrument_id}: unexpected {label} node(s) found: {count}"


def _assert_run_id_logged(
    log_entries: list[dict[str, object]], *, action: str, run_id: str, entity_id: str
) -> None:
    matches = [
        entry
        for entry in log_entries
        if entry.get("action") == action and entry.get("run_id") == run_id
    ]
    assert matches, f"no log entry found for action={action!r} run_id={run_id!r}"
    assert any(
        entry.get("entity_id") == entity_id and entry.get("outcome") == "succeeded"
        for entry in matches
    ), f"no succeeded entry with entity_id={entity_id!r} for action={action!r} run_id={run_id!r}"


@pytest.mark.falkordb_live
@pytest.mark.llm_live
@pytest.mark.skipif(
    not _LLM_INTERFACE_MODEL,
    reason="requires .env sourced (PS_LLMINTERFACE_MODEL, AZURE_API_KEY, AZURE_API_BASE)",
)
def test_live_three_regulation_capstone_extracts_and_derives_across_cra_gdpr_nis2(
    make_emitter: MakeEmitter, read_lines: ReadLines
) -> None:
    assert _LLM_INTERFACE_MODEL is not None  # narrows type; skipif already guards this
    model = _LLM_INTERFACE_MODEL

    # load_config() still resolves falkordb_host/port correctly here — only
    # PS_LLMINTERFACE_MODEL/PS_LLMINTERFACE_EMBED_MODEL are stripped by the
    # autouse fixture, not PS_FALKORDB_HOST/PORT.
    config = load_config()
    db = connect_from_config(config)
    adapter = CellarEliDomainMappingAdapter()
    emitter, log_path = make_emitter(filename=_LOG_FILENAME)

    outcomes = {
        fixture.short_name: _run_pipeline_for_regulatory_instrument(
            db, adapter, fixture, model=model, emitter=emitter
        )
        for fixture in _REGULATIONS
    }

    emitter.flush()
    log_entries = read_lines(log_path)

    for fixture in _REGULATIONS:
        outcome = outcomes[fixture.short_name]
        baseline_graph = select_graph(db, baseline_graph_name(fixture.short_name))

        _assert_ac001_provenance(
            baseline_graph, outcome.native_citation_refs, fixture.regulatory_instrument_id
        )
        _assert_ac002_confidence(baseline_graph, fixture.regulatory_instrument_id)
        _assert_ac003_derivation_shape(
            baseline_graph,
            outcome.derivation_result.unmatched_requirement_ids,
            outcome.derivation_result.unmatched_obligation_ids,
            fixture.regulatory_instrument_id,
        )
        assert isinstance(outcome.derivation_result.unmatched_requirement_ids, tuple)  # AC-004
        assert isinstance(outcome.derivation_result.unmatched_obligation_ids, tuple)  # AC-BI-002
        _assert_ac005_regulatory_instrument_scope(baseline_graph, fixture.regulatory_instrument_id)
        _assert_ac008_no_governance_nodes(baseline_graph, fixture.regulatory_instrument_id)
        _assert_run_id_logged(
            log_entries,
            action="extract_roles_and_requirements",
            run_id=outcome.extraction_run_id,
            entity_id=fixture.regulatory_instrument_id,
        )
        _assert_run_id_logged(
            log_entries,
            action="derive_obligations_and_capabilities",
            run_id=outcome.derivation_run_id,
            entity_id=fixture.regulatory_instrument_id,
        )

    all_run_ids = [outcome.extraction_run_id for outcome in outcomes.values()] + [
        outcome.derivation_run_id for outcome in outcomes.values()
    ]
    assert len(set(all_run_ids)) == len(all_run_ids), (
        f"expected 6 mutually distinct run_ids across the 3 regulations' 2 actions each, "
        f"got {all_run_ids}"
    )


def _annex_i_defines_and_expresses_rows(
    baseline_graph: GraphHandle,
) -> tuple[list[list[object]], list[list[object]]]:
    """Mirrors `_assert_ac001_provenance`'s query pattern (lines 228-250
    above), filtered down to rows whose `source_ref` is exactly `"Annex I"`.
    """
    defines_rows = _query_rows(
        baseline_graph,
        "MATCH (:RegulatoryInstrument)-[e:DEFINES]->(:Role) "
        'WHERE e.source_ref = "Annex I" RETURN e.source_ref',
    )
    expresses_rows = _query_rows(
        baseline_graph,
        "MATCH (:RegulatoryInstrument)-[e:EXPRESSES]->(r:Requirement) "
        'WHERE e.source_ref = "Annex I" RETURN e.source_ref, r.confidence',
    )
    return defines_rows, expresses_rows


@pytest.mark.falkordb_live
@pytest.mark.llm_live
@pytest.mark.skipif(
    not _LLM_INTERFACE_MODEL,
    reason="requires .env sourced (PS_LLMINTERFACE_MODEL, AZURE_API_KEY, AZURE_API_BASE)",
)
def test_live_cra_annex_i_produces_at_least_one_role_and_requirement(
    make_emitter: MakeEmitter,
) -> None:
    """AC-BI-006: running `ExtractRolesAndRequirements` against CRA's real
    native graph, scoped to only its ANNEX units via
    `_AnnexOnlyDomainMappingAdapter`, persists at least one Role and one
    Requirement to `cra_baseline` whose `DEFINES`/`EXPRESSES` `source_ref`
    is exactly `"Annex I"`.

    The Requirement `confidence` values collected here are also persisted
    to a module-level cache (`_CRA_ANNEX_I_REQUIREMENT_CONFIDENCES`) that
    Slice 11's `test_live_nis2_annex_content_yields_no_hallucinated_duties`
    reads, so the "materially lower-confidence" comparison in that test has
    real CRA Annex I confidences to compare against without re-running
    extraction. If this test has not run in the same pytest session (e.g.
    it was deselected via `-k`), Slice 11's test falls back to re-querying
    `cra_baseline` directly for the same values — see that test's docstring.
    """
    assert _LLM_INTERFACE_MODEL is not None  # narrows type; skipif already guards this
    model = _LLM_INTERFACE_MODEL

    config = load_config()
    db = connect_from_config(config)
    adapter = _AnnexOnlyDomainMappingAdapter(CellarEliDomainMappingAdapter())
    emitter, _log_path = make_emitter(filename=_LOG_FILENAME)

    fixture = _RegulatoryInstrumentFixture("CRA", "CRA-1.0")
    native_graph = select_graph(db, native_graph_name(fixture.short_name))
    baseline_graph = select_graph(db, baseline_graph_name(fixture.short_name))

    with bind_run_context("capstone-cra-annex-i-extraction"):
        extract_roles_and_requirements(
            fixture.regulatory_instrument_id,
            adapter=adapter,
            native_graph=native_graph,
            baseline_graph=baseline_graph,
            model=model,
            emitter=emitter,
        )

    defines_rows, expresses_rows = _annex_i_defines_and_expresses_rows(baseline_graph)

    assert defines_rows, (
        f"{fixture.regulatory_instrument_id}: no DEFINES edge with source_ref == 'Annex I' found"
    )
    assert expresses_rows, (
        f"{fixture.regulatory_instrument_id}: no EXPRESSES edge with source_ref == 'Annex I' found"
    )

    confidences = tuple(cast("float", row[1]) for row in expresses_rows)
    _CRA_ANNEX_I_REQUIREMENT_CONFIDENCES.clear()
    _CRA_ANNEX_I_REQUIREMENT_CONFIDENCES.extend(confidences)


# Populated by `test_live_cra_annex_i_produces_at_least_one_role_and_requirement` when it
# runs in the same pytest session; read by
# `test_live_nis2_annex_content_yields_no_hallucinated_duties` as a same-session shortcut
# before falling back to a direct `cra_baseline` re-query. Both tests share this module's
# `.env`-presence skipif, so either both run or both skip together in a normal invocation
# (e.g. `-m "llm_live and falkordb_live"` with no `-k` filter) — the fallback exists only
# to keep Slice 11's test correct under an unusual `-k`-filtered or single-test invocation.
_CRA_ANNEX_I_REQUIREMENT_CONFIDENCES: list[float] = []


@pytest.mark.falkordb_live
@pytest.mark.llm_live
@pytest.mark.skipif(
    not _LLM_INTERFACE_MODEL,
    reason="requires .env sourced (PS_LLMINTERFACE_MODEL, AZURE_API_KEY, AZURE_API_BASE)",
)
def test_live_nis2_annex_content_yields_no_hallucinated_duties(
    make_emitter: MakeEmitter,
) -> None:
    """AC-BI-011 (and the runtime half of AC-BI-005): running the identical
    `_AnnexOnlyDomainMappingAdapter` code path — no regulation-specific
    branch anywhere in `cellar_eli.py` or `extraction.py` — against NIS2's
    real native graph extracts no hallucinated Requirement duties from its
    non-operative annex content.

    Concrete, non-arbitrary threshold (per PLAN.md Slice 11): this test
    passes if EITHER (a) zero `Requirement` nodes whose `EXPRESSES.source_ref`
    starts with `"Annex "` exist in `nis2_baseline`, OR (b) at least one
    such Requirement exists but
    `max(nis2_annex_confidences) < min(cra_annex_i_confidences)` — i.e.
    NIS2's single most-confident annex-derived Requirement is still less
    confident than CRA's single least-confident genuine Annex I Requirement
    (from `test_live_cra_annex_i_produces_at_least_one_role_and_requirement`,
    which must run first in the same session — a normal
    `-m "llm_live and falkordb_live"` invocation with no `-k` filter runs
    both; if that module-level cache is empty here — e.g. this test was
    selected on its own — this test re-queries `cra_baseline` directly for
    the same `source_ref = "Annex I"` Requirement confidences instead of
    skipping the comparison).
    """
    assert _LLM_INTERFACE_MODEL is not None  # narrows type; skipif already guards this
    model = _LLM_INTERFACE_MODEL

    config = load_config()
    db = connect_from_config(config)
    adapter = _AnnexOnlyDomainMappingAdapter(CellarEliDomainMappingAdapter())
    emitter, _log_path = make_emitter(filename=_LOG_FILENAME)

    fixture = _RegulatoryInstrumentFixture("NIS2", "NIS2-1.0")
    native_graph = select_graph(db, native_graph_name(fixture.short_name))
    baseline_graph = select_graph(db, baseline_graph_name(fixture.short_name))

    with bind_run_context("capstone-nis2-annex-extraction"):
        extract_roles_and_requirements(
            fixture.regulatory_instrument_id,
            adapter=adapter,
            native_graph=native_graph,
            baseline_graph=baseline_graph,
            model=model,
            emitter=emitter,
        )

    nis2_annex_rows = _query_rows(
        baseline_graph,
        "MATCH (:RegulatoryInstrument)-[e:EXPRESSES]->(r:Requirement) "
        'WHERE e.source_ref STARTS WITH "Annex " RETURN e.source_ref, r.confidence',
    )
    if not nis2_annex_rows:
        return  # branch (a): no annex-derived Requirement candidates at all

    nis2_annex_confidences = [cast("float", row[1]) for row in nis2_annex_rows]

    if _CRA_ANNEX_I_REQUIREMENT_CONFIDENCES:
        cra_annex_i_confidences = list(_CRA_ANNEX_I_REQUIREMENT_CONFIDENCES)
    else:
        cra_baseline_graph = select_graph(db, baseline_graph_name("CRA"))
        _cra_defines_rows, cra_expresses_rows = _annex_i_defines_and_expresses_rows(
            cra_baseline_graph
        )
        cra_annex_i_confidences = [cast("float", row[1]) for row in cra_expresses_rows]

    assert cra_annex_i_confidences, (
        "no CRA Annex I Requirement confidences available for comparison — "
        "run test_live_cra_annex_i_produces_at_least_one_role_and_requirement first"
    )

    # branch (b): NIS2's most-confident annex-derived Requirement must still be
    # strictly less confident than CRA's least-confident genuine Annex I Requirement.
    assert max(nis2_annex_confidences) < min(cra_annex_i_confidences), (
        f"NIS2 annex-derived Requirement confidences {nis2_annex_confidences} are not "
        f"materially lower than CRA Annex I confidences {cra_annex_i_confidences} "
        f"(max(nis2) < min(cra) failed)"
    )
