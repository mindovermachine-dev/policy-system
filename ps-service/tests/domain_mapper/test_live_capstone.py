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
regulation's extraction is capped to the first `_LIMIT_PER_REGULATION`
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
from typing import cast

import pytest
from falkordb import FalkorDB

from ps_service.config import load_config
from ps_service.domain_mapper.adapters.base import DomainMappingAdapter
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
from ps_service.domain_mapper.models import DerivationResult, ExtractionUnit
from ps_service.logging import LogEmitter, bind_run_context

_LIMIT_PER_REGULATION = 15
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
class _RegulationFixture:
    short_name: str
    regulation_id: str


_REGULATIONS = (
    _RegulationFixture("CRA", "CRA-1.0"),
    _RegulationFixture("GDPR", "GDPR-1.0"),
    _RegulationFixture("NIS2", "NIS2-1.0"),
)


class _LimitedDomainMappingAdapter:
    """Wraps a real `DomainMappingAdapter`, capping the `ExtractionUnit`s
    returned to the first `limit` (the inner adapter's own document order).
    Satisfies `DomainMappingAdapter` structurally — no production code
    change needed for this test's bounding requirement."""

    def __init__(self, inner: DomainMappingAdapter, limit: int) -> None:
        self._inner = inner
        self._limit = limit

    def read_native_units(self, graph: GraphHandle) -> tuple[ExtractionUnit, ...]:
        return self._inner.read_native_units(graph)[: self._limit]


@dataclass
class _RegulationOutcome:
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
    return {cast(str, row[0]) for row in article_refs} | {
        cast(str, row[0]) for row in paragraph_refs
    }


def _run_pipeline_for_regulation(
    db: FalkorDB,
    adapter: DomainMappingAdapter,
    fixture: _RegulationFixture,
    *,
    model: str,
    emitter: LogEmitter,
) -> _RegulationOutcome:
    """Runs both actions for one regulation, each inside its own bound run
    context. A different `run_id` is bound for extraction vs. derivation —
    no orchestrator wires a single shared `run_id` across both actions yet
    (Open Question 6), so this test binds one per action per regulation
    (6 distinct run_ids total) to exercise AC-006/AC-007 unambiguously
    rather than leave the "same or different" choice implicit."""
    native_graph = select_graph(db, native_graph_name(fixture.short_name))
    baseline_graph = select_graph(db, baseline_graph_name(fixture.short_name))
    limited_adapter = _LimitedDomainMappingAdapter(adapter, _LIMIT_PER_REGULATION)
    native_citation_refs = _native_citation_refs(native_graph)

    extraction_run_id = f"capstone-{fixture.short_name.lower()}-extraction"
    with bind_run_context(extraction_run_id):
        extract_roles_and_requirements(
            fixture.regulation_id,
            adapter=limited_adapter,
            native_graph=native_graph,
            baseline_graph=baseline_graph,
            model=model,
            emitter=emitter,
        )

    derivation_run_id = f"capstone-{fixture.short_name.lower()}-derivation"
    with bind_run_context(derivation_run_id):
        derivation_result = derive_obligations_and_capabilities(
            fixture.regulation_id,
            baseline_graph=baseline_graph,
            model=model,
            emitter=emitter,
        )

    return _RegulationOutcome(
        native_citation_refs=native_citation_refs,
        derivation_result=derivation_result,
        extraction_run_id=extraction_run_id,
        derivation_run_id=derivation_run_id,
    )


def _assert_ac001_provenance(
    baseline_graph: GraphHandle, native_refs: set[str], regulation_id: str
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
    assert defines_refs, f"{regulation_id}: no DEFINES edges written"
    assert expresses_refs, f"{regulation_id}: no EXPRESSES edges written"
    for ref in (*defines_refs, *expresses_refs):
        assert ref in native_refs, (
            f"{regulation_id}: source_ref {ref!r} does not match any native-graph element"
        )


def _assert_ac002_confidence(baseline_graph: GraphHandle, regulation_id: str) -> None:
    role_confidences = [
        row[0] for row in _query_rows(baseline_graph, "MATCH (n:Role) RETURN n.confidence")
    ]
    requirement_confidences = [
        row[0]
        for row in _query_rows(baseline_graph, "MATCH (n:Requirement) RETURN n.confidence")
    ]
    all_confidences = role_confidences + requirement_confidences
    assert all_confidences, f"{regulation_id}: no confidence-bearing Role/Requirement nodes found"
    for value in all_confidences:
        confidence = cast(float, value)
        assert 0.0 <= confidence <= 1.0, f"{regulation_id}: confidence out of range: {confidence!r}"
    # Low-confidence existence is explicitly non-blocking (Open Question 8) —
    # deliberately no assertion either way on whether one happens to appear.


def _assert_ac003_derivation_shape(
    baseline_graph: GraphHandle, unmatched_ids: tuple[str, ...], regulation_id: str
) -> None:
    requirement_rows = _query_rows(
        baseline_graph,
        "MATCH (req:Requirement) OPTIONAL MATCH (req)-[s:SATISFIED_BY]->(:Obligation) "
        "RETURN req.id, count(s)",
    )
    for requirement_id_value, satisfied_count in requirement_rows:
        if requirement_id_value in unmatched_ids:
            continue
        assert cast(int, satisfied_count) >= 1, (
            f"{regulation_id}: Requirement {requirement_id_value!r} has no SATISFIED_BY edge"
        )

    has_rows = _query_rows(
        baseline_graph,
        "MATCH (o:Obligation) OPTIONAL MATCH (:Role)-[h:HAS]->(o) RETURN o.id, count(h)",
    )
    for obligation_id_value, has_count in has_rows:
        assert cast(int, has_count) == 1, (
            f"{regulation_id}: Obligation {obligation_id_value!r} has {has_count} HAS "
            "edges, expected exactly 1"
        )

    requires_rows = _query_rows(
        baseline_graph,
        "MATCH (o:Obligation) OPTIONAL MATCH (o)-[r:REQUIRES]->(:Capability) RETURN o.id, count(r)",
    )
    for obligation_id_value, requires_count in requires_rows:
        assert cast(int, requires_count) >= 1, (
            f"{regulation_id}: Obligation {obligation_id_value!r} has no REQUIRES edge"
        )


def _assert_ac005_regulation_scope(baseline_graph: GraphHandle, regulation_id: str) -> None:
    regulation_rows = _query_rows(baseline_graph, "MATCH (r:RegulatoryInstrument) RETURN r.id")
    assert regulation_rows == [[regulation_id]], (
        f"unexpected Regulation node set in {regulation_id}'s baseline graph: {regulation_rows}"
    )

    requirement_ids = [
        row[0] for row in _query_rows(baseline_graph, "MATCH (n:Requirement) RETURN n.id")
    ]
    prefix = f"{regulation_id}_req_art_"
    for requirement_id_value in requirement_ids:
        assert cast(str, requirement_id_value).startswith(prefix), (
            f"Requirement {requirement_id_value!r} in {regulation_id}'s baseline graph does not "
            "carry that regulation's own id prefix — possible cross-regulation contamination"
        )


def _assert_ac008_no_governance_nodes(baseline_graph: GraphHandle, regulation_id: str) -> None:
    for label in _GOVERNANCE_LABELS:
        count = _query_rows(baseline_graph, f"MATCH (n:{label}) RETURN count(n)")[0][0]
        assert count == 0, f"{regulation_id}: unexpected {label} node(s) found: {count}"


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
    make_emitter, read_lines
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
        fixture.short_name: _run_pipeline_for_regulation(
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
            baseline_graph, outcome.native_citation_refs, fixture.regulation_id
        )
        _assert_ac002_confidence(baseline_graph, fixture.regulation_id)
        _assert_ac003_derivation_shape(
            baseline_graph,
            outcome.derivation_result.unmatched_requirement_ids,
            fixture.regulation_id,
        )
        assert isinstance(outcome.derivation_result.unmatched_requirement_ids, tuple)  # AC-004
        _assert_ac005_regulation_scope(baseline_graph, fixture.regulation_id)
        _assert_ac008_no_governance_nodes(baseline_graph, fixture.regulation_id)
        _assert_run_id_logged(
            log_entries,
            action="extract_roles_and_requirements",
            run_id=outcome.extraction_run_id,
            entity_id=fixture.regulation_id,
        )
        _assert_run_id_logged(
            log_entries,
            action="derive_obligations_and_capabilities",
            run_id=outcome.derivation_run_id,
            entity_id=fixture.regulation_id,
        )

    all_run_ids = [outcome.extraction_run_id for outcome in outcomes.values()] + [
        outcome.derivation_run_id for outcome in outcomes.values()
    ]
    assert len(set(all_run_ids)) == len(all_run_ids), (
        f"expected 6 mutually distinct run_ids across the 3 regulations' 2 actions each, "
        f"got {all_run_ids}"
    )
