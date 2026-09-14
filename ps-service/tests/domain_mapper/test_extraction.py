"""Tests for ps_service.domain_mapper.extraction._extract_candidates_for_unit.

Per PLAN_REVIEWED.md §11 Increment 7 / the binding testing convention
(§0.3/§0.5): `call_completion` is faked with a hand-written structural fake
satisfying `CompletionCaller`'s Protocol (`llm_interface/client.py`), not
`unittest.mock.Mock`/`MagicMock` — mirrors
`tests/llm_interface/test_route_completion_mocked.py`'s exact style.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

import httpx
import openai
import pytest
from litellm.types.utils import Choices, Message, ModelResponse

from ps_service.domain_mapper import identity
from ps_service.domain_mapper.errors import DomainMapperExtractionError
from ps_service.domain_mapper.extraction import (
    _build_defined_terms_map,  # pyright: ignore[reportPrivateUsage]  # test drives this module-internal helper directly (see module docstring)
    _build_requirement_graph,  # pyright: ignore[reportPrivateUsage]  # test drives this module-internal helper directly (see module docstring)
    _canonicalize_roles,  # pyright: ignore[reportPrivateUsage]  # test drives this module-internal helper directly (see module docstring)
    _extract_all_defined_terms,  # pyright: ignore[reportPrivateUsage]  # test drives this module-internal helper directly (see module docstring)
    _extract_candidates_for_unit,  # pyright: ignore[reportPrivateUsage]  # test drives this module-internal helper directly (see module docstring)
    _extract_defined_terms_for_unit,  # pyright: ignore[reportPrivateUsage]  # test drives this module-internal helper directly (see module docstring)
    _is_definitions_unit,  # pyright: ignore[reportPrivateUsage]  # test drives this module-internal helper directly (see module docstring)
    _normalize_term,  # pyright: ignore[reportPrivateUsage]  # test drives this module-internal helper directly (see module docstring)
    _pool_defined_terms,  # pyright: ignore[reportPrivateUsage]  # test drives this module-internal helper directly (see module docstring)
    _read_regulatory_instrument_properties,  # pyright: ignore[reportPrivateUsage]  # test drives this module-internal helper directly (see module docstring)
    extract_roles_and_requirements,
)
from ps_service.domain_mapper.models import (
    DefinedTermCandidate,
    ExtractionUnit,
    RequirementCandidate,
)
from ps_service.domain_mapper.prompts import (
    DEFINITIONS_EXTRACTION_SYSTEM_PROMPT,
    EXTRACTION_SYSTEM_PROMPT,
)
from ps_service.llm_interface.errors import LlmProviderError
from ps_service.logging import bind_run_context

if TYPE_CHECKING:
    from domain_mapper._fakes import MakeEmitter, ReadLines
    from ps_service.domain_mapper.falkordb_client import GraphHandle
    from ps_service.llm_interface.client import CompletionCaller

_REGULATION_ID = "CRA-1.0"

_UNIT = ExtractionUnit(
    citation_ref="Art. 13(1)",
    text="The manufacturer shall conduct a cybersecurity risk assessment.",
    article_number="13",
    paragraph_number="1",
    article_heading="Obligations of manufacturers",
)


def _model_response(content: str) -> ModelResponse:
    return ModelResponse(
        id="x",
        model="fake-model",
        choices=[
            Choices(
                finish_reason="stop", index=0, message=Message(content=content, role="assistant")
            )
        ],
    )


def test_extract_candidates_for_unit_returns_candidates_from_scripted_response(
    make_emitter: MakeEmitter,
) -> None:
    emitter, _log_path = make_emitter()
    scripted_text = json.dumps(
        {
            "requirements": [
                {
                    "role_name": "Manufacturer",
                    "text": "Conduct a cybersecurity risk assessment.",
                    "type": "requirement",
                    "letter_suffix": None,
                    "confidence": 0.92,
                }
            ]
        }
    )

    def fake_call_completion(
        *, model: str, messages: list[dict[str, str]], timeout: float
    ) -> ModelResponse:
        return _model_response(scripted_text)

    candidates = _extract_candidates_for_unit(
        _UNIT, model="fake-model", call_completion=fake_call_completion, emitter=emitter
    )

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.role_name == "Manufacturer"
    assert candidate.text == "Conduct a cybersecurity risk assessment."
    assert candidate.type == "requirement"
    assert candidate.confidence == 0.92
    assert candidate.unit_citation_ref == "Art. 13(1)"
    assert candidate.unit_article_number == "13"
    assert candidate.unit_paragraph_number == "1"


def test_extract_candidates_for_unit_sends_system_and_user_messages(
    make_emitter: MakeEmitter,
) -> None:
    """The system prompt and the unit's own text are sent as two distinct
    messages, with the unit's text delimited (not concatenated into the
    system prompt) — L2's untrusted-content rule.
    """
    emitter, _log_path = make_emitter()
    captured: dict[str, list[dict[str, str]]] = {}

    def fake_call_completion(
        *, model: str, messages: list[dict[str, str]], timeout: float
    ) -> ModelResponse:
        captured["messages"] = messages
        return _model_response(json.dumps({"requirements": []}))

    _extract_candidates_for_unit(
        _UNIT, model="fake-model", call_completion=fake_call_completion, emitter=emitter
    )

    sent = captured["messages"]
    assert [m["role"] for m in sent] == ["system", "user"]
    assert "Art. 13(1)" in sent[1]["content"]
    assert _UNIT.text in sent[1]["content"]
    # The unit's text never leaks into the system message.
    assert _UNIT.text not in sent[0]["content"]


def test_extract_candidates_for_unit_propagates_llm_provider_error_unchanged(
    make_emitter: MakeEmitter,
) -> None:
    """An infra failure calling the LLM at all (route_completion's own
    LlmProviderError) is not caught here — it propagates unchanged, the
    infra-vs-content failure split PLAN_REVIEWED.md §5.2 requires.
    """
    emitter, _log_path = make_emitter()

    def fake_call_completion(
        *, model: str, messages: list[dict[str, str]], timeout: float
    ) -> ModelResponse:
        raise openai.APIConnectionError(request=httpx.Request("POST", "https://example.invalid"))

    with pytest.raises(LlmProviderError):
        _extract_candidates_for_unit(
            _UNIT, model="fake-model", call_completion=fake_call_completion, emitter=emitter
        )


def test_extract_candidates_for_unit_propagates_extraction_error_for_malformed_response(
    make_emitter: MakeEmitter,
) -> None:
    """A malformed/unparseable LLM *response* (as opposed to an infra
    failure) surfaces as DomainMapperExtractionError, naming the unit —
    this function does not swallow it; per-unit failure isolation is the
    orchestrating extract_roles_and_requirements's job, not this one's.
    """
    emitter, _log_path = make_emitter()

    def fake_call_completion(
        *, model: str, messages: list[dict[str, str]], timeout: float
    ) -> ModelResponse:
        return _model_response("{not valid json")

    with pytest.raises(DomainMapperExtractionError) as exc_info:
        _extract_candidates_for_unit(
            _UNIT, model="fake-model", call_completion=fake_call_completion, emitter=emitter
        )
    assert "Art. 13(1)" in str(exc_info.value)


# --- _canonicalize_roles (Increment 8) -------------------------------------


def _candidate(**overrides: object) -> RequirementCandidate:
    fields: dict[str, object] = {
        "unit_citation_ref": "Art. 13(1)",
        "unit_article_number": "13",
        "unit_paragraph_number": "1",
        "role_name": "Manufacturer",
        "text": "Manufacturers shall conduct a cybersecurity risk assessment.",
        "type": "requirement",
        "letter_suffix": None,
        "confidence": 0.9,
    }
    fields.update(overrides)
    return RequirementCandidate.model_validate(fields)


def test_canonicalize_roles_collapses_same_role_name_onto_one_node() -> None:
    """Two candidates naming the same role_name -> one Role node, and the
    DEFINES edge carries the FIRST candidate's citation_ref, not the
    second's.
    """
    first = _candidate(unit_citation_ref="Art. 13(1)", role_name="Manufacturer")
    second = _candidate(unit_citation_ref="Art. 14(2)", role_name="Manufacturer")

    role_nodes, role_edges, role_node_ids, fallback_roles = _canonicalize_roles(
        [first, second], _REGULATION_ID, {}
    )

    assert len(role_nodes) == 1
    assert len(role_edges) == 1
    assert role_edges[0].source_ref == "Art. 13(1)"
    assert role_edges[0].role_node_id == role_nodes[0].id
    assert role_node_ids == {"Manufacturer": role_nodes[0].id}
    assert fallback_roles == ((role_nodes[0].id, "Manufacturer"),)


def test_canonicalize_roles_produces_distinct_nodes_for_distinct_role_names() -> None:
    manufacturer = _candidate(role_name="Manufacturer")
    importer = _candidate(role_name="Importer")

    role_nodes, role_edges, role_node_ids, fallback_roles = _canonicalize_roles(
        [manufacturer, importer], _REGULATION_ID, {}
    )

    assert len(role_nodes) == 2
    assert len(role_edges) == 2
    assert role_node_ids["Manufacturer"] != role_node_ids["Importer"]
    assert fallback_roles == (
        (role_node_ids["Manufacturer"], "Manufacturer"),
        (role_node_ids["Importer"], "Importer"),
    )


def test_canonicalize_roles_stamps_first_candidates_confidence() -> None:
    first = _candidate(role_name="Manufacturer", confidence=0.4)
    second = _candidate(role_name="Manufacturer", confidence=0.95)

    role_nodes, _role_edges, _role_node_ids, fallback_roles = _canonicalize_roles(
        [first, second], _REGULATION_ID, {}
    )

    assert role_nodes[0].properties["confidence"] == 0.4
    assert fallback_roles == ((role_nodes[0].id, "Manufacturer"),)


def test_canonicalize_roles_uses_matched_defined_term_citation_ref() -> None:
    """AC-BI-004: a matched defined term's citation_ref wins over the
    candidate's own unit_citation_ref, and no fallback is recorded.
    """
    candidate = _candidate(role_name="Manufacturer", unit_citation_ref="Art. 13(1)")

    role_nodes, role_edges, _role_node_ids, fallback_roles = _canonicalize_roles(
        [candidate], _REGULATION_ID, {"manufacturer": "Art. 2(1)"}
    )

    assert role_edges[0].source_ref == "Art. 2(1)"
    assert role_edges[0].role_node_id == role_nodes[0].id
    assert fallback_roles == ()


def test_canonicalize_roles_matches_case_insensitively_and_whitespace_normalized() -> None:
    """AC-BI-005: the role_name is normalized (casefold + whitespace
    collapse) via `_normalize_term` before the `defined_terms` lookup.
    """
    candidate = _candidate(role_name="Data   Controller", unit_citation_ref="Art. 13(1)")
    defined_terms = {_normalize_term("data controller"): "Art. 4(7)"}

    _role_nodes, role_edges, _role_node_ids, fallback_roles = _canonicalize_roles(
        [candidate], _REGULATION_ID, defined_terms
    )

    assert role_edges[0].source_ref == "Art. 4(7)"
    assert fallback_roles == ()


def test_canonicalize_roles_falls_back_when_defined_terms_map_is_empty() -> None:
    """AC-BI-008: an empty defined_terms map falls back to the candidate's
    own unit_citation_ref, and records the fallback.
    """
    candidate = _candidate(role_name="Manufacturer", unit_citation_ref="Art. 13(1)")

    role_nodes, role_edges, _role_node_ids, fallback_roles = _canonicalize_roles(
        [candidate], _REGULATION_ID, {}
    )

    assert role_edges[0].source_ref == "Art. 13(1)"
    assert fallback_roles == ((role_nodes[0].id, "Manufacturer"),)


def test_canonicalize_roles_falls_back_for_role_with_no_matching_term() -> None:
    """AC-BI-009: a non-empty defined_terms map that simply has no entry for
    this role_name still falls back to the candidate's own unit_citation_ref.
    """
    candidate = _candidate(role_name="Manufacturer", unit_citation_ref="Art. 13(1)")

    role_nodes, role_edges, _role_node_ids, fallback_roles = _canonicalize_roles(
        [candidate], _REGULATION_ID, {"importer": "Art. 2(2)"}
    )

    assert role_edges[0].source_ref == "Art. 13(1)"
    assert fallback_roles == ((role_nodes[0].id, "Manufacturer"),)


def test_canonicalize_roles_only_first_occurrence_determines_match_or_fallback() -> None:
    """First-candidate-wins (mirrors `..._stamps_first_candidates_confidence`)
    also governs the DEFINES source_ref/fallback decision: the second
    candidate's unit_citation_ref and match/fallback status are irrelevant.
    """
    first = _candidate(role_name="Manufacturer", unit_citation_ref="Art. 13(1)", confidence=0.4)
    second = _candidate(role_name="Manufacturer", unit_citation_ref="Art. 99(9)", confidence=0.95)

    role_nodes, role_edges, _role_node_ids, fallback_roles = _canonicalize_roles(
        [first, second], _REGULATION_ID, {}
    )

    assert role_edges[0].source_ref == "Art. 13(1)"
    assert fallback_roles == ((role_nodes[0].id, "Manufacturer"),)


# --- _build_requirement_graph (Increment 8, B2 fix) ------------------------


def test_build_requirement_graph_deduplicates_identical_id_and_text() -> None:
    """A trivial same-input sanity check (two candidates that happen to
    compute the same id and carry byte-identical text within one call) —
    NOT proof of cross-call idempotent re-extraction, which is out of this
    test's scope.
    """
    text = "Manufacturers shall conduct a cybersecurity risk assessment."
    first = _candidate(text=text)
    second = _candidate(text=text)
    role_node_ids = {"Manufacturer": "role_manufacturer_abc123"}

    nodes, edges, collided_ids = _build_requirement_graph(
        [first, second], _REGULATION_ID, role_node_ids
    )

    assert len(nodes) == 1
    assert len(edges) == 1
    assert collided_ids == ()


def test_build_requirement_graph_disambiguates_same_id_different_text() -> None:
    """No exception is ever raised for a Requirement-id collision — two
    Requirement nodes are persisted, the second carries id `f"{base_id}#2"`,
    both original texts are preserved unchanged, and collided_ids names the
    disambiguated id.
    """
    first = _candidate(text="Conduct a cybersecurity risk assessment.")
    second = _candidate(text="Report vulnerabilities without undue delay.")
    role_node_ids = {"Manufacturer": "role_manufacturer_abc123"}

    nodes, edges, collided_ids = _build_requirement_graph(
        [first, second], _REGULATION_ID, role_node_ids
    )

    assert len(nodes) == 2
    assert len(edges) == 2
    base_id = nodes[0].id
    assert nodes[1].id == f"{base_id}#2"
    assert nodes[0].properties["text"] == "Conduct a cybersecurity risk assessment."
    assert nodes[1].properties["text"] == "Report vulnerabilities without undue delay."
    assert collided_ids == (f"{base_id}#2",)


def test_build_requirement_graph_disambiguates_three_distinct_texts_deterministically() -> None:
    """Three candidates at the same base id, three different texts -> ids
    base_id, base_id#2, base_id#3 in document order — proves the
    disambiguation is deterministic given a fixed candidate order, not just
    a two-way case.
    """
    first = _candidate(text="Text A")
    second = _candidate(text="Text B")
    third = _candidate(text="Text C")
    role_node_ids = {"Manufacturer": "role_manufacturer_abc123"}

    nodes, _edges, collided_ids = _build_requirement_graph(
        [first, second, third], _REGULATION_ID, role_node_ids
    )

    base_id = nodes[0].id
    assert [node.id for node in nodes] == [base_id, f"{base_id}#2", f"{base_id}#3"]
    assert collided_ids == (f"{base_id}#2", f"{base_id}#3")


def test_build_requirement_graph_stamps_role_id_bookkeeping_property() -> None:
    candidate = _candidate(role_name="Manufacturer")
    role_node_ids = {"Manufacturer": "role_manufacturer_abc123"}

    nodes, edges, _collided_ids = _build_requirement_graph(
        [candidate], _REGULATION_ID, role_node_ids
    )

    assert nodes[0].properties["role_id"] == "role_manufacturer_abc123"
    assert edges[0].source_ref == candidate.unit_citation_ref
    assert edges[0].requirement_node_id == nodes[0].id


# --- extract_roles_and_requirements (Increment 10) -------------------------
#
# Hand-written structural fakes throughout — no unittest.mock, per the
# binding testing convention (PLAN_REVIEWED.md §0.3/§0.5).


@dataclass
class _RecordedCall:
    query: str
    params: dict[str, object] | None


class _FakeQueryResult:
    """Satisfies `GraphQueryResult` structurally."""

    def __init__(self, result_set: list[object]) -> None:
        self._result_set = result_set

    @property
    def result_set(self) -> list[object]:
        return self._result_set


class _FakeRegulatoryInstrumentNode:
    """Satisfies extraction.py's own `_RegulatoryInstrumentNode` Protocol structurally
    — only `.properties` is ever read.
    """

    def __init__(self, properties: dict[str, object]) -> None:
        self.properties = properties


class _FakeNativeGraph:
    """Satisfies `GraphHandle` structurally. Answers
    `MATCH (r:RegulatoryInstrument) RETURN r` with a scripted Regulation node;
    ignores any other query (the fake adapter never actually queries it in
    these tests).
    """

    def __init__(self, regulatory_instrument_properties: dict[str, object]) -> None:
        self._regulatory_instrument_properties = regulatory_instrument_properties

    def query(self, q: str, params: dict[str, object] | None = None) -> _FakeQueryResult:
        return _FakeQueryResult(
            [[_FakeRegulatoryInstrumentNode(self._regulatory_instrument_properties)]]
        )


class _FakeBaselineGraph:
    """Satisfies `GraphHandle` structurally, capturing every `(query,
    params)` call for assertion — mirrors `test_graph_writer.py`'s
    `_FakeGraph`.
    """

    def __init__(self) -> None:
        self.calls: list[_RecordedCall] = []

    def query(self, q: str, params: dict[str, object] | None = None) -> _FakeQueryResult:
        self.calls.append(_RecordedCall(q, params))
        return _FakeQueryResult([[0]])


class _FakeAdapter:
    """Satisfies `DomainMappingAdapter` structurally — returns a
    pre-scripted tuple of `ExtractionUnit`s regardless of `graph`.
    """

    def __init__(self, units: tuple[ExtractionUnit, ...]) -> None:
        self._units = units

    def read_native_units(self, graph: GraphHandle) -> tuple[ExtractionUnit, ...]:
        return self._units


def _scripted_call_completion(responses: dict[str, str | Exception]) -> CompletionCaller:
    """A `CompletionCaller` fake keyed by the unit's `citation_ref`, which
    `_build_extraction_messages` always embeds in the user message as
    `"Citation: {citation_ref}"` — lets a test script one canned response
    (or an exception to raise) per unit regardless of call order.
    """

    def _call(*, model: str, messages: list[dict[str, str]], timeout: float) -> ModelResponse:
        user_content = messages[1]["content"]
        for citation_ref, response in responses.items():
            if f"Citation: {citation_ref}" in user_content:
                if isinstance(response, Exception):
                    raise response
                return _model_response(response)
        raise AssertionError(f"no scripted response for message: {user_content!r}")

    return _call


def _requirements_json(
    *, role_name: str, text: str, letter_suffix: str | None = None, confidence: float = 0.9
) -> str:
    return json.dumps(
        {
            "requirements": [
                {
                    "role_name": role_name,
                    "text": text,
                    "type": "requirement",
                    "letter_suffix": letter_suffix,
                    "confidence": confidence,
                }
            ]
        }
    )


_UNIT_MANUFACTURER = ExtractionUnit(
    citation_ref="Art. 13(1)",
    text="The manufacturer shall conduct a cybersecurity risk assessment.",
    article_number="13",
    paragraph_number="1",
    article_heading="Obligations of manufacturers",
)
_UNIT_IMPORTER = ExtractionUnit(
    citation_ref="Art. 14(2)",
    text="The importer shall verify the manufacturer's conformity assessment.",
    article_number="14",
    paragraph_number="2",
    article_heading="Obligations of importers",
)


def _find_edge_call(graph: _FakeBaselineGraph, relationship_type: str) -> list[_RecordedCall]:
    return [call for call in graph.calls if f"[e:{relationship_type}]" in call.query]


def test_extract_roles_and_requirements_ac001_produces_role_and_requirement_edges_with_source_ref(
    make_emitter: MakeEmitter,
) -> None:
    """AC-001: two units, two different roles -> Role/Requirement nodes with
    DEFINES/EXPRESSES edges carrying source_ref == unit.citation_ref.
    """
    emitter, _log_path = make_emitter()
    native_graph = _FakeNativeGraph({"id": _REGULATION_ID, "title": "Cyber Resilience Act"})
    baseline_graph = _FakeBaselineGraph()
    adapter = _FakeAdapter((_UNIT_MANUFACTURER, _UNIT_IMPORTER))
    call_completion = _scripted_call_completion(
        {
            _UNIT_MANUFACTURER.citation_ref: _requirements_json(
                role_name="Manufacturer", text="Conduct a cybersecurity risk assessment."
            ),
            _UNIT_IMPORTER.citation_ref: _requirements_json(
                role_name="Importer", text="Verify the manufacturer's conformity assessment."
            ),
        }
    )

    result = extract_roles_and_requirements(
        _REGULATION_ID,
        adapter=adapter,
        native_graph=native_graph,
        baseline_graph=baseline_graph,
        model="fake-model",
        call_completion=call_completion,
        emitter=emitter,
    )

    assert result.candidate_count == 2
    assert result.skipped_unit_count == 0
    assert set(result.role_node_ids) == {"Manufacturer", "Importer"}
    assert len(result.requirement_ids) == 2

    defines_calls = _find_edge_call(baseline_graph, "DEFINES")
    expresses_calls = _find_edge_call(baseline_graph, "EXPRESSES")
    assert len(defines_calls) == 2
    assert len(expresses_calls) == 2
    defines_source_refs = {call.params["source_ref"] for call in defines_calls if call.params}
    expresses_source_refs = {call.params["source_ref"] for call in expresses_calls if call.params}
    assert defines_source_refs == {_UNIT_MANUFACTURER.citation_ref, _UNIT_IMPORTER.citation_ref}
    assert expresses_source_refs == {_UNIT_MANUFACTURER.citation_ref, _UNIT_IMPORTER.citation_ref}


def test_extract_roles_and_requirements_ac002_persists_low_confidence_candidate(
    make_emitter: MakeEmitter,
) -> None:
    """AC-002: a candidate with confidence=0.1 is still persisted, not filtered."""
    emitter, _log_path = make_emitter()
    native_graph = _FakeNativeGraph({"id": _REGULATION_ID})
    baseline_graph = _FakeBaselineGraph()
    adapter = _FakeAdapter((_UNIT_MANUFACTURER,))
    call_completion = _scripted_call_completion(
        {
            _UNIT_MANUFACTURER.citation_ref: _requirements_json(
                role_name="Manufacturer",
                text="Conduct a cybersecurity risk assessment.",
                confidence=0.1,
            )
        }
    )

    result = extract_roles_and_requirements(
        _REGULATION_ID,
        adapter=adapter,
        native_graph=native_graph,
        baseline_graph=baseline_graph,
        model="fake-model",
        call_completion=call_completion,
        emitter=emitter,
    )

    assert result.candidate_count == 1
    assert len(result.requirement_ids) == 1
    requirement_calls = [
        call
        for call in baseline_graph.calls
        if call.query == "MERGE (n:Requirement {id: $id}) SET n += $properties"
    ]
    assert len(requirement_calls) == 1
    assert requirement_calls[0].params is not None
    properties = requirement_calls[0].params["properties"]
    assert isinstance(properties, dict)
    assert properties["confidence"] == 0.1


_UNIT_ANNEX_I = ExtractionUnit(
    citation_ref="Annex I",
    text="Products with digital elements shall be designed, developed and "
    "produced in such a way that they ensure an appropriate level of "
    "cybersecurity based on the risks.",
    article_number="I",
    paragraph_number="1",
    article_heading="",
)


def test_extract_roles_and_requirements_persists_from_annex_unit(
    make_emitter: MakeEmitter,
) -> None:
    """Slice 6 (GH #25): `extract_roles_and_requirements` is annex-agnostic —
    a single annex-derived `ExtractionUnit` (`citation_ref="Annex I"`,
    `article_number="I"`) flows through the same real orchestrating
    function, unmodified, as any ARTICLE-derived unit (mirrors
    `test_extract_roles_and_requirements_ac001_...` exactly, just with one
    annex unit instead of two article units).

    Proves end-to-end (adapter-shaped input -> extraction -> identity ->
    graph-write payload), not just in isolation: the persisted
    `DEFINES`/`EXPRESSES` edges carry `source_ref == "Annex I"` verbatim
    (AC-BI-001/003), and the persisted `RequirementNode.id` equals the real
    `identity.requirement_id()` formula computed independently here from
    the unit's own `article_number`/`paragraph_number` (AC-BI-007) — not a
    hardcoded string, so a regression in either the id formula or its
    annex-unit wiring would fail this assertion.
    """
    emitter, _log_path = make_emitter()
    native_graph = _FakeNativeGraph({"id": _REGULATION_ID, "title": "Cyber Resilience Act"})
    baseline_graph = _FakeBaselineGraph()
    adapter = _FakeAdapter((_UNIT_ANNEX_I,))
    call_completion = _scripted_call_completion(
        {
            _UNIT_ANNEX_I.citation_ref: _requirements_json(
                role_name="Manufacturer",
                text="Ensure products are designed with an appropriate level of cybersecurity.",
            )
        }
    )

    result = extract_roles_and_requirements(
        _REGULATION_ID,
        adapter=adapter,
        native_graph=native_graph,
        baseline_graph=baseline_graph,
        model="fake-model",
        call_completion=call_completion,
        emitter=emitter,
    )

    assert result.candidate_count == 1
    assert result.skipped_unit_count == 0
    assert set(result.role_node_ids) == {"Manufacturer"}

    expected_requirement_id = identity.requirement_id(_REGULATION_ID, "I", "1", None)
    assert result.requirement_ids == (expected_requirement_id,)

    defines_calls = _find_edge_call(baseline_graph, "DEFINES")
    expresses_calls = _find_edge_call(baseline_graph, "EXPRESSES")
    assert len(defines_calls) == 1
    assert len(expresses_calls) == 1
    assert defines_calls[0].params is not None
    assert expresses_calls[0].params is not None
    assert defines_calls[0].params["source_ref"] == "Annex I"
    assert expresses_calls[0].params["source_ref"] == "Annex I"

    requirement_calls = [
        call
        for call in baseline_graph.calls
        if call.query == "MERGE (n:Requirement {id: $id}) SET n += $properties"
    ]
    assert len(requirement_calls) == 1
    assert requirement_calls[0].params is not None
    assert requirement_calls[0].params["id"] == expected_requirement_id


def test_extract_roles_and_requirements_ac006_emits_log_entry_with_bound_run_id(
    make_emitter: MakeEmitter, read_lines: ReadLines
) -> None:
    """AC-006: with a run context bound, the emitted outcome="succeeded"
    entry carries the bound run_id — mirrors
    test_route_completion_logs_run_id.py exactly.
    """
    emitter, log_path = make_emitter()
    native_graph = _FakeNativeGraph({"id": _REGULATION_ID})
    baseline_graph = _FakeBaselineGraph()
    adapter = _FakeAdapter((_UNIT_MANUFACTURER,))
    call_completion = _scripted_call_completion(
        {
            _UNIT_MANUFACTURER.citation_ref: _requirements_json(
                role_name="Manufacturer", text="Conduct a cybersecurity risk assessment."
            )
        }
    )

    with bind_run_context("run-x"):
        extract_roles_and_requirements(
            _REGULATION_ID,
            adapter=adapter,
            native_graph=native_graph,
            baseline_graph=baseline_graph,
            model="fake-model",
            call_completion=call_completion,
            emitter=emitter,
        )
    emitter.flush()

    lines = read_lines(log_path)
    succeeded_entries = [
        line
        for line in lines
        if line.get("component") == "domain_mapper" and line.get("outcome") == "succeeded"
    ]
    assert len(succeeded_entries) == 1
    assert succeeded_entries[0]["run_id"] == "run-x"
    assert succeeded_entries[0]["action"] == "extract_roles_and_requirements"
    assert succeeded_entries[0]["entity_id"] == _REGULATION_ID


def test_extract_roles_and_requirements_isolates_per_unit_extraction_failure(
    make_emitter: MakeEmitter, read_lines: ReadLines
) -> None:
    """One of two units' scripted response is malformed JSON -> the OTHER
    unit's candidates still persist, an outcome="error" entry is emitted
    for the bad unit's citation_ref, and skipped_unit_count == 1.
    """
    emitter, log_path = make_emitter()
    native_graph = _FakeNativeGraph({"id": _REGULATION_ID})
    baseline_graph = _FakeBaselineGraph()
    adapter = _FakeAdapter((_UNIT_MANUFACTURER, _UNIT_IMPORTER))
    call_completion = _scripted_call_completion(
        {
            _UNIT_MANUFACTURER.citation_ref: _requirements_json(
                role_name="Manufacturer", text="Conduct a cybersecurity risk assessment."
            ),
            _UNIT_IMPORTER.citation_ref: "{not valid json",
        }
    )

    result = extract_roles_and_requirements(
        _REGULATION_ID,
        adapter=adapter,
        native_graph=native_graph,
        baseline_graph=baseline_graph,
        model="fake-model",
        call_completion=call_completion,
        emitter=emitter,
    )
    emitter.flush()

    assert result.skipped_unit_count == 1
    assert result.candidate_count == 1
    assert len(result.requirement_ids) == 1

    lines = read_lines(log_path)
    error_entries = [
        line
        for line in lines
        if line.get("component") == "domain_mapper" and line.get("outcome") == "error"
    ]
    assert len(error_entries) == 1
    assert error_entries[0]["entity_id"] == _UNIT_IMPORTER.citation_ref


def test_malformed_annex_unit_response_is_skipped_and_logged(
    make_emitter: MakeEmitter, read_lines: ReadLines
) -> None:
    """Slice 7 (GH #25): the same per-unit failure isolation proven for two
    ARTICLE units above (`..._isolates_per_unit_extraction_failure`) also
    covers an annex-derived unit correctly, with no production change --
    a malformed/unparseable LLM response for the annex unit
    (`citation_ref="Annex I"`) is caught, logged
    (`outcome="error"`, `entity_id="Annex I"`), and does not abort the
    ordinary ARTICLE unit's own extraction.
    """
    emitter, log_path = make_emitter()
    native_graph = _FakeNativeGraph({"id": _REGULATION_ID})
    baseline_graph = _FakeBaselineGraph()
    adapter = _FakeAdapter((_UNIT_MANUFACTURER, _UNIT_ANNEX_I))
    call_completion = _scripted_call_completion(
        {
            _UNIT_MANUFACTURER.citation_ref: _requirements_json(
                role_name="Manufacturer", text="Conduct a cybersecurity risk assessment."
            ),
            _UNIT_ANNEX_I.citation_ref: "{not valid json",
        }
    )

    result = extract_roles_and_requirements(
        _REGULATION_ID,
        adapter=adapter,
        native_graph=native_graph,
        baseline_graph=baseline_graph,
        model="fake-model",
        call_completion=call_completion,
        emitter=emitter,
    )
    emitter.flush()

    assert result.skipped_unit_count == 1
    assert result.candidate_count == 1
    assert len(result.requirement_ids) == 1
    assert set(result.role_node_ids) == {"Manufacturer"}

    lines = read_lines(log_path)
    error_entries = [
        line
        for line in lines
        if line.get("component") == "domain_mapper" and line.get("outcome") == "error"
    ]
    assert len(error_entries) == 1
    assert error_entries[0]["entity_id"] == "Annex I"


def test_extract_roles_and_requirements_logs_error_kind_without_raw_payload(
    make_emitter: MakeEmitter, read_lines: ReadLines
) -> None:
    """AC-BI-002: the outcome="error" log entry's `extra` carries `error_kind`
    matching the raised `DomainMapperExtractionError`, and the raw LLM
    payload (or any substring of it) never reaches the logged line -- the
    catch site must never pass `str(exc)`/the exception's message (which
    embeds the raw payload) to `emit_log_entry`.
    """
    marker = "MARKER_SECRET_PAYLOAD_9f3e7c1b2a"
    emitter, log_path = make_emitter()
    native_graph = _FakeNativeGraph({"id": _REGULATION_ID})
    baseline_graph = _FakeBaselineGraph()
    adapter = _FakeAdapter((_UNIT_IMPORTER,))
    call_completion = _scripted_call_completion(
        {_UNIT_IMPORTER.citation_ref: json.dumps({"requirements": marker})}
    )

    extract_roles_and_requirements(
        _REGULATION_ID,
        adapter=adapter,
        native_graph=native_graph,
        baseline_graph=baseline_graph,
        model="fake-model",
        call_completion=call_completion,
        emitter=emitter,
    )
    emitter.flush()

    lines = read_lines(log_path)
    error_entries = [
        line
        for line in lines
        if line.get("component") == "domain_mapper" and line.get("outcome") == "error"
    ]
    assert len(error_entries) == 1
    assert error_entries[0]["error_kind"] == "non_list_requirements"

    raw_log_text = log_path.read_text(encoding="utf-8")
    assert marker not in raw_log_text


def test_extract_roles_and_requirements_propagates_llm_provider_error_and_aborts(
    make_emitter: MakeEmitter,
) -> None:
    """An LlmProviderError from one unit's call propagates and aborts the
    whole call — infra failures are not per-unit-isolated (no partial-write
    guarantee claimed either way).
    """
    emitter, _log_path = make_emitter()
    native_graph = _FakeNativeGraph({"id": _REGULATION_ID})
    baseline_graph = _FakeBaselineGraph()
    adapter = _FakeAdapter((_UNIT_MANUFACTURER, _UNIT_IMPORTER))
    call_completion = _scripted_call_completion(
        {
            _UNIT_MANUFACTURER.citation_ref: _requirements_json(
                role_name="Manufacturer", text="Conduct a cybersecurity risk assessment."
            ),
            _UNIT_IMPORTER.citation_ref: openai.APIConnectionError(
                request=httpx.Request("POST", "https://example.invalid")
            ),
        }
    )

    with pytest.raises(LlmProviderError):
        extract_roles_and_requirements(
            _REGULATION_ID,
            adapter=adapter,
            native_graph=native_graph,
            baseline_graph=baseline_graph,
            model="fake-model",
            call_completion=call_completion,
            emitter=emitter,
        )


def test_extract_roles_and_requirements_zero_units_returns_well_formed_all_zero_result(
    make_emitter: MakeEmitter,
) -> None:
    """Q2 fix: an adapter returning zero units is not an error — the
    Regulation node is still MERGEd, and a well-formed all-zero
    ExtractionResult is returned.
    """
    emitter, _log_path = make_emitter()
    native_graph = _FakeNativeGraph({"id": _REGULATION_ID})
    baseline_graph = _FakeBaselineGraph()
    adapter = _FakeAdapter(())

    result = extract_roles_and_requirements(
        _REGULATION_ID,
        adapter=adapter,
        native_graph=native_graph,
        baseline_graph=baseline_graph,
        model="fake-model",
        call_completion=_scripted_call_completion({}),
        emitter=emitter,
    )

    assert result.candidate_count == 0
    assert result.skipped_unit_count == 0
    assert result.role_node_ids == {}
    assert result.requirement_ids == ()
    assert result.requirement_id_collisions == ()
    assert len(baseline_graph.calls) == 1
    assert (
        baseline_graph.calls[0].query
        == "MERGE (n:RegulatoryInstrument {id: $id}) SET n += $properties"
    )


def test_extract_roles_and_requirements_surfaces_collision_without_aborting(
    make_emitter: MakeEmitter, read_lines: ReadLines
) -> None:
    """B2 fix: two units whose candidates land on the same requirement_id
    with different text -> both Requirement nodes persisted (one
    #2-suffixed), requirement_id_collisions is non-empty, an
    outcome="collision" entry is emitted naming the base id — no exception,
    no aborted call.
    """
    emitter, log_path = make_emitter()
    native_graph = _FakeNativeGraph({"id": _REGULATION_ID})
    baseline_graph = _FakeBaselineGraph()
    # Two distinct units that happen to resolve to the same (article,
    # paragraph) location, so their candidates collide at requirement_id().
    unit_a = ExtractionUnit(
        citation_ref="Art. 13(1)-a",
        text="The manufacturer shall conduct a cybersecurity risk assessment.",
        article_number="13",
        paragraph_number="1",
        article_heading="Obligations of manufacturers",
    )
    unit_b = ExtractionUnit(
        citation_ref="Art. 13(1)-b",
        text="The manufacturer shall conduct a cybersecurity risk assessment (duplicate source).",
        article_number="13",
        paragraph_number="1",
        article_heading="Obligations of manufacturers",
    )
    adapter = _FakeAdapter((unit_a, unit_b))
    call_completion = _scripted_call_completion(
        {
            unit_a.citation_ref: _requirements_json(
                role_name="Manufacturer", text="Conduct a cybersecurity risk assessment."
            ),
            unit_b.citation_ref: _requirements_json(
                role_name="Manufacturer", text="Report vulnerabilities without undue delay."
            ),
        }
    )

    result = extract_roles_and_requirements(
        _REGULATION_ID,
        adapter=adapter,
        native_graph=native_graph,
        baseline_graph=baseline_graph,
        model="fake-model",
        call_completion=call_completion,
        emitter=emitter,
    )
    emitter.flush()

    assert len(result.requirement_ids) == 2
    assert result.requirement_id_collisions != ()
    collided_id = result.requirement_id_collisions[0]
    assert collided_id in result.requirement_ids

    requirement_calls = [
        call
        for call in baseline_graph.calls
        if call.query == "MERGE (n:Requirement {id: $id}) SET n += $properties"
    ]
    assert len(requirement_calls) == 2

    lines = read_lines(log_path)
    collision_entries = [
        line
        for line in lines
        if line.get("component") == "domain_mapper" and line.get("outcome") == "collision"
    ]
    assert len(collision_entries) == 1
    assert collision_entries[0]["entity_id"] == collided_id


# --- _read_regulatory_instrument_properties: whole-bag read, instrument_type included --


def test_read_regulatory_instrument_properties_includes_instrument_type() -> None:
    """AC-BI-010 (Domain Mapper, read side): `_read_regulatory_instrument_properties`
    returns `dict(node.properties)` — the whole property bag, no field
    filter — so `instrument_type` is carried through with NO src change.
    """
    native_graph = _FakeNativeGraph(
        {"id": "NIS2-1.0", "title": "NIS2 Directive", "instrument_type": "directive"}
    )

    result = _read_regulatory_instrument_properties(native_graph, "NIS2-1.0")

    assert result["instrument_type"] == "directive"


# --- _is_definitions_unit (issue #26, AC-BI-002) ---------------------------


def _unit_with_heading(article_heading: str) -> ExtractionUnit:
    return ExtractionUnit(
        citation_ref="Art. 2",
        text="irrelevant for this test",
        article_number="2",
        paragraph_number="1",
        article_heading=article_heading,
    )


@pytest.mark.parametrize(
    ("article_heading", "expected"),
    [
        ("DEFINITIONS", True),
        ("Article 2 — Definitions", True),
        ("Scope and definitions", True),
        ("Obligations of manufacturers", False),
        # FLAW-5: a word-boundary regex, not a plain substring check --
        # "definitions" is not its own word here ("re" + "definitions" are
        # contiguous \w characters), so this must NOT match.
        ("Redefinitions of Scope", False),
    ],
)
def test_is_definitions_unit_matches_case_insensitively(
    article_heading: str, *, expected: bool
) -> None:
    assert _is_definitions_unit(_unit_with_heading(article_heading)) is expected


def _scripted_call_completion_by_prompt_kind(
    responses: dict[tuple[str, Literal["duty", "definitions"]], str | Exception],
) -> CompletionCaller:
    """A `CompletionCaller` fake keyed on `(citation_ref, prompt_kind)`.

    Per CHANGES.md's FLAW-1: `_scripted_call_completion` keys solely on
    `citation_ref` (via the user message), so it cannot return different
    canned responses for the duty-pass and definitions-pass calls that both
    fire against the SAME unit's `citation_ref` within one test. This
    helper classifies each call's `prompt_kind` from the system message
    (`messages[0]["content"]`) — `"duty"` for `EXTRACTION_SYSTEM_PROMPT`,
    `"definitions"` for `DEFINITIONS_EXTRACTION_SYSTEM_PROMPT` — then looks
    up `(citation_ref, prompt_kind)` using the same
    `f"Citation: {citation_ref}"` substring match against the user message
    as `_scripted_call_completion` does.
    """

    def _call(*, model: str, messages: list[dict[str, str]], timeout: float) -> ModelResponse:
        system_content = messages[0]["content"]
        if system_content == EXTRACTION_SYSTEM_PROMPT:
            prompt_kind: Literal["duty", "definitions"] = "duty"
        elif system_content == DEFINITIONS_EXTRACTION_SYSTEM_PROMPT:
            prompt_kind = "definitions"
        else:
            raise AssertionError(f"unrecognized system prompt: {system_content!r}")
        user_content = messages[1]["content"]
        for (citation_ref, kind), response in responses.items():
            if kind == prompt_kind and f"Citation: {citation_ref}" in user_content:
                if isinstance(response, Exception):
                    raise response
                return _model_response(response)
        raise AssertionError(
            f"no scripted response for ({prompt_kind!r}, message): {user_content!r}"
        )

    return _call


def test_extract_roles_and_requirements_still_extracts_duties_from_a_definitions_headed_unit(
    make_emitter: MakeEmitter,
) -> None:
    """AC-BI-002: a definitions-headed unit is not excluded from the
    unchanged duty-extraction traversal — `_is_definitions_unit` is purely
    additive and does not reorder/filter `_extract_all_candidates`'s own
    unit traversal.

    `_build_defined_terms_map` is not wired into
    `extract_roles_and_requirements` in this dispatch, so only the duty
    pass ever actually fires for this unit here — but the compound-keyed
    (FLAW-1) fixture is used regardless, since this unit's citation_ref is
    one `_is_definitions_unit` would also flag for the (not-yet-wired)
    definitions pass, and a future dispatch wiring it in must not silently
    break this test's scripted responses.
    """
    emitter, _log_path = make_emitter()
    unit = ExtractionUnit(
        citation_ref="Art. 2(1)",
        text=(
            "For the purposes of this Regulation, 'manufacturer' means any natural or "
            "legal person. The manufacturer shall conduct a cybersecurity risk assessment."
        ),
        article_number="2",
        paragraph_number="1",
        article_heading="Definitions",
    )
    assert _is_definitions_unit(unit) is True
    native_graph = _FakeNativeGraph({"id": _REGULATION_ID})
    baseline_graph = _FakeBaselineGraph()
    adapter = _FakeAdapter((unit,))
    call_completion = _scripted_call_completion_by_prompt_kind(
        {
            (unit.citation_ref, "duty"): _requirements_json(
                role_name="Manufacturer", text="Conduct a cybersecurity risk assessment."
            ),
            (unit.citation_ref, "definitions"): json.dumps({"terms": ["Manufacturer"]}),
        }
    )

    result = extract_roles_and_requirements(
        _REGULATION_ID,
        adapter=adapter,
        native_graph=native_graph,
        baseline_graph=baseline_graph,
        model="fake-model",
        call_completion=call_completion,
        emitter=emitter,
    )

    assert result.candidate_count == 1
    assert len(result.requirement_ids) == 1


# --- _normalize_term (issue #26, AC-BI-005) --------------------------------


def test_normalize_term_casefolds_and_collapses_whitespace() -> None:
    assert _normalize_term("  Data   Controller ") == "data controller"


# --- _extract_defined_terms_for_unit (issue #26, AC-BI-001/003) ------------

_DEFINITIONS_UNIT = ExtractionUnit(
    citation_ref="Art. 2(1)",
    text="'Manufacturer' means any natural or legal person who develops products.",
    article_number="2",
    paragraph_number="1",
    article_heading="Definitions",
)
_DEFINITIONS_UNIT_2 = ExtractionUnit(
    citation_ref="Art. 2(2)",
    text=(
        "'Importer' means any natural or legal person established in the Union who "
        "places on the market a product from a third country."
    ),
    article_number="2",
    paragraph_number="2",
    article_heading="Definitions",
)


def test_extract_defined_terms_for_unit_returns_candidates_from_scripted_response(
    make_emitter: MakeEmitter,
) -> None:
    emitter, _log_path = make_emitter()
    scripted_text = json.dumps({"terms": ["Manufacturer"]})

    def fake_call_completion(
        *, model: str, messages: list[dict[str, str]], timeout: float
    ) -> ModelResponse:
        return _model_response(scripted_text)

    candidates = _extract_defined_terms_for_unit(
        _DEFINITIONS_UNIT,
        model="fake-model",
        call_completion=fake_call_completion,
        emitter=emitter,
    )

    assert len(candidates) == 1
    assert candidates[0].term == "Manufacturer"
    assert candidates[0].citation_ref == _DEFINITIONS_UNIT.citation_ref


def test_extract_defined_terms_for_unit_sends_system_and_user_messages(
    make_emitter: MakeEmitter,
) -> None:
    """The system prompt and the unit's own text are sent as two distinct
    messages, with the unit's text delimited (not concatenated into the
    system prompt) — L2's untrusted-content rule.
    """
    emitter, _log_path = make_emitter()
    captured: dict[str, list[dict[str, str]]] = {}

    def fake_call_completion(
        *, model: str, messages: list[dict[str, str]], timeout: float
    ) -> ModelResponse:
        captured["messages"] = messages
        return _model_response(json.dumps({"terms": []}))

    _extract_defined_terms_for_unit(
        _DEFINITIONS_UNIT,
        model="fake-model",
        call_completion=fake_call_completion,
        emitter=emitter,
    )

    sent = captured["messages"]
    assert [m["role"] for m in sent] == ["system", "user"]
    assert sent[0]["content"] == DEFINITIONS_EXTRACTION_SYSTEM_PROMPT
    assert _DEFINITIONS_UNIT.citation_ref in sent[1]["content"]
    assert _DEFINITIONS_UNIT.text in sent[1]["content"]
    # The unit's text never leaks into the system message.
    assert _DEFINITIONS_UNIT.text not in sent[0]["content"]


def test_extract_defined_terms_for_unit_propagates_llm_provider_error_unchanged(
    make_emitter: MakeEmitter,
) -> None:
    """An infra failure calling the LLM at all is not caught here — it
    propagates unchanged, mirroring `_extract_candidates_for_unit`'s own
    infra-vs-content failure split.
    """
    emitter, _log_path = make_emitter()

    def fake_call_completion(
        *, model: str, messages: list[dict[str, str]], timeout: float
    ) -> ModelResponse:
        raise openai.APIConnectionError(request=httpx.Request("POST", "https://example.invalid"))

    with pytest.raises(LlmProviderError):
        _extract_defined_terms_for_unit(
            _DEFINITIONS_UNIT,
            model="fake-model",
            call_completion=fake_call_completion,
            emitter=emitter,
        )


def test_extract_defined_terms_for_unit_propagates_extraction_error_for_malformed_response(
    make_emitter: MakeEmitter,
) -> None:
    """A malformed/unparseable LLM response surfaces as
    `DomainMapperExtractionError`, naming the unit — this function does not
    swallow it; per-unit failure isolation is `_extract_all_defined_terms`'s
    job, not this one's.
    """
    emitter, _log_path = make_emitter()

    def fake_call_completion(
        *, model: str, messages: list[dict[str, str]], timeout: float
    ) -> ModelResponse:
        return _model_response("{not valid json")

    with pytest.raises(DomainMapperExtractionError) as exc_info:
        _extract_defined_terms_for_unit(
            _DEFINITIONS_UNIT,
            model="fake-model",
            call_completion=fake_call_completion,
            emitter=emitter,
        )
    assert _DEFINITIONS_UNIT.citation_ref in str(exc_info.value)


# --- _extract_all_defined_terms (issue #26, AC-BI-003/007, design decision 4) --


def test_extract_all_defined_terms_issues_zero_llm_calls_for_zero_units(
    make_emitter: MakeEmitter,
) -> None:
    emitter, _log_path = make_emitter()

    def fail_call_completion(
        *, model: str, messages: list[dict[str, str]], timeout: float
    ) -> ModelResponse:
        raise AssertionError("should not be called for zero definitions units")

    result = _extract_all_defined_terms(
        (), model="fake-model", call_completion=fail_call_completion, emitter=emitter
    )

    assert result == []


def test_extract_all_defined_terms_issues_exactly_one_call_per_qualifying_unit(
    make_emitter: MakeEmitter,
) -> None:
    emitter, _log_path = make_emitter()
    call_count = 0
    scripted = _scripted_call_completion(
        {
            _DEFINITIONS_UNIT.citation_ref: json.dumps({"terms": ["Manufacturer"]}),
            _DEFINITIONS_UNIT_2.citation_ref: json.dumps({"terms": ["Importer"]}),
        }
    )

    def counting_call_completion(
        *, model: str, messages: list[dict[str, str]], timeout: float
    ) -> ModelResponse:
        nonlocal call_count
        call_count += 1
        return scripted(model=model, messages=messages, timeout=timeout)

    result = _extract_all_defined_terms(
        (_DEFINITIONS_UNIT, _DEFINITIONS_UNIT_2),
        model="fake-model",
        call_completion=counting_call_completion,
        emitter=emitter,
    )

    assert call_count == 2
    assert {candidate.term for candidate in result} == {"Manufacturer", "Importer"}


def test_extract_all_defined_terms_isolates_per_unit_failure(
    make_emitter: MakeEmitter, read_lines: ReadLines
) -> None:
    """One of two definitions units' scripted response is malformed JSON ->
    the OTHER unit's terms still come back, one outcome="error" entry is
    emitted naming the bad unit's citation_ref, and no exception propagates.
    """
    emitter, log_path = make_emitter()
    scripted = _scripted_call_completion(
        {
            _DEFINITIONS_UNIT.citation_ref: json.dumps({"terms": ["Manufacturer"]}),
            _DEFINITIONS_UNIT_2.citation_ref: "{not valid json",
        }
    )

    result = _extract_all_defined_terms(
        (_DEFINITIONS_UNIT, _DEFINITIONS_UNIT_2),
        model="fake-model",
        call_completion=scripted,
        emitter=emitter,
    )
    emitter.flush()

    assert [candidate.term for candidate in result] == ["Manufacturer"]

    lines = read_lines(log_path)
    error_entries = [
        line
        for line in lines
        if line.get("component") == "domain_mapper" and line.get("outcome") == "error"
    ]
    assert len(error_entries) == 1
    assert error_entries[0]["entity_id"] == _DEFINITIONS_UNIT_2.citation_ref


# --- _pool_defined_terms (issue #26, AC-BI-005/006, design decision 5) -----


def test_pool_defined_terms_pools_across_multiple_candidates() -> None:
    candidates = [
        DefinedTermCandidate(term="Manufacturer", citation_ref="Art. 2(1)"),
        DefinedTermCandidate(term="Importer", citation_ref="Art. 2(2)"),
    ]

    pooled = _pool_defined_terms(candidates)

    assert pooled == {"manufacturer": "Art. 2(1)", "importer": "Art. 2(2)"}


def test_pool_defined_terms_matches_case_insensitively_and_whitespace_normalized() -> None:
    candidates = [DefinedTermCandidate(term="Data   Controller", citation_ref="Art. 4(7)")]

    pooled = _pool_defined_terms(candidates)

    assert pooled[_normalize_term("data controller")] == "Art. 4(7)"


def test_pool_defined_terms_first_occurrence_wins_on_duplicate_normalized_term() -> None:
    candidates = [
        DefinedTermCandidate(term="Manufacturer", citation_ref="Art. 2(1)"),
        DefinedTermCandidate(term="manufacturer", citation_ref="Art. 3(4)"),
    ]

    pooled = _pool_defined_terms(candidates)

    assert pooled["manufacturer"] == "Art. 2(1)"


# --- _build_defined_terms_map (issue #26, composes the above) --------------


def test_build_defined_terms_map_end_to_end_with_mocked_llm(
    make_emitter: MakeEmitter,
) -> None:
    """AC-BI-002/003/005/006/007 together: a duty unit + two definitions
    units -> the returned map has exactly 2 keys (the duty unit contributed
    nothing, proving the detection filter) and the LLM was called exactly
    twice (proving exactly the qualifying units were called, not the duty
    unit too).
    """
    emitter, _log_path = make_emitter()
    duty_unit = ExtractionUnit(
        citation_ref="Art. 13(1)",
        text="The manufacturer shall conduct a cybersecurity risk assessment.",
        article_number="13",
        paragraph_number="1",
        article_heading="Obligations of manufacturers",
    )
    call_count = 0
    scripted = _scripted_call_completion(
        {
            _DEFINITIONS_UNIT.citation_ref: json.dumps({"terms": ["Manufacturer"]}),
            _DEFINITIONS_UNIT_2.citation_ref: json.dumps({"terms": ["Importer"]}),
        }
    )

    def counting_call_completion(
        *, model: str, messages: list[dict[str, str]], timeout: float
    ) -> ModelResponse:
        nonlocal call_count
        call_count += 1
        return scripted(model=model, messages=messages, timeout=timeout)

    result = _build_defined_terms_map(
        (duty_unit, _DEFINITIONS_UNIT, _DEFINITIONS_UNIT_2),
        model="fake-model",
        call_completion=counting_call_completion,
        emitter=emitter,
    )

    assert call_count == 2
    assert result == {"manufacturer": "Art. 2(1)", "importer": "Art. 2(2)"}


def test_build_defined_terms_map_zero_definitions_units_issues_zero_llm_calls(
    make_emitter: MakeEmitter,
) -> None:
    emitter, _log_path = make_emitter()
    duty_unit = ExtractionUnit(
        citation_ref="Art. 13(1)",
        text="The manufacturer shall conduct a cybersecurity risk assessment.",
        article_number="13",
        paragraph_number="1",
        article_heading="Obligations of manufacturers",
    )

    def fail_call_completion(
        *, model: str, messages: list[dict[str, str]], timeout: float
    ) -> ModelResponse:
        raise AssertionError("should not be called when no definitions units exist")

    result = _build_defined_terms_map(
        (duty_unit,), model="fake-model", call_completion=fail_call_completion, emitter=emitter
    )

    assert result == {}


# --- extract_roles_and_requirements: definitions wiring (issue #26, Slice 6) --
#
# End-to-end tests proving `_build_defined_terms_map` is actually wired into
# `extract_roles_and_requirements`, its output threaded into
# `_canonicalize_roles`, and the `definitions_fallback` log entries emitted
# per AC-BI-010. Per CHANGES.md's FLAW-1, any test below that mixes a
# definitions unit with duty units on the SAME citation_ref must use
# `_scripted_call_completion_by_prompt_kind`; tests using separate units for
# definitions vs. duty roles use the plain `_scripted_call_completion`.


def test_extract_roles_and_requirements_sets_defines_source_ref_from_definitions_unit(
    make_emitter: MakeEmitter,
) -> None:
    """AC-BI-004, end-to-end: a dedicated definitions unit defines
    "Manufacturer" -> the persisted DEFINES edge's source_ref is the
    definitions unit's own citation_ref, not the duty unit's.
    """
    emitter, _log_path = make_emitter()
    native_graph = _FakeNativeGraph({"id": _REGULATION_ID})
    baseline_graph = _FakeBaselineGraph()
    definitions_unit = ExtractionUnit(
        citation_ref="Art. 2(1)",
        text="'Manufacturer' means any natural or legal person who develops products.",
        article_number="2",
        paragraph_number="1",
        article_heading="Definitions",
    )
    adapter = _FakeAdapter((definitions_unit, _UNIT_MANUFACTURER))
    # FLAW-1: `definitions_unit`'s citation_ref is fed to BOTH passes — the
    # duty pass traverses ALL units (including definitions-headed ones, per
    # AC-BI-002), and the definitions pass traverses just this one — so the
    # compound-keyed fixture is required, not the plain single-keyed one.
    call_completion = _scripted_call_completion_by_prompt_kind(
        {
            (definitions_unit.citation_ref, "definitions"): json.dumps({"terms": ["Manufacturer"]}),
            (definitions_unit.citation_ref, "duty"): json.dumps({"requirements": []}),
            (_UNIT_MANUFACTURER.citation_ref, "duty"): _requirements_json(
                role_name="Manufacturer", text="Conduct a cybersecurity risk assessment."
            ),
        }
    )

    extract_roles_and_requirements(
        _REGULATION_ID,
        adapter=adapter,
        native_graph=native_graph,
        baseline_graph=baseline_graph,
        model="fake-model",
        call_completion=call_completion,
        emitter=emitter,
    )

    defines_calls = _find_edge_call(baseline_graph, "DEFINES")
    assert len(defines_calls) == 1
    assert defines_calls[0].params is not None
    assert defines_calls[0].params["source_ref"] == definitions_unit.citation_ref


def test_extract_roles_and_requirements_falls_back_to_first_duty_occurrence_when_no_definitions_unit_exists(  # noqa: E501 - name mirrors PLAN.md Slice 6 verbatim
    make_emitter: MakeEmitter, read_lines: ReadLines
) -> None:
    """AC-BI-008, end-to-end: no definitions-headed unit anywhere -> the
    DEFINES edge falls back to the Role's own first duty unit_citation_ref
    (regression-proves unchanged behavior), and exactly one
    outcome="definitions_fallback" log entry is emitted, naming the Role and
    the RegulatoryInstrument.
    """
    emitter, log_path = make_emitter()
    native_graph = _FakeNativeGraph({"id": _REGULATION_ID})
    baseline_graph = _FakeBaselineGraph()
    adapter = _FakeAdapter((_UNIT_MANUFACTURER,))
    call_completion = _scripted_call_completion(
        {
            _UNIT_MANUFACTURER.citation_ref: _requirements_json(
                role_name="Manufacturer", text="Conduct a cybersecurity risk assessment."
            )
        }
    )

    result = extract_roles_and_requirements(
        _REGULATION_ID,
        adapter=adapter,
        native_graph=native_graph,
        baseline_graph=baseline_graph,
        model="fake-model",
        call_completion=call_completion,
        emitter=emitter,
    )
    emitter.flush()

    defines_calls = _find_edge_call(baseline_graph, "DEFINES")
    assert len(defines_calls) == 1
    assert defines_calls[0].params is not None
    assert defines_calls[0].params["source_ref"] == _UNIT_MANUFACTURER.citation_ref

    lines = read_lines(log_path)
    fallback_entries = [
        line
        for line in lines
        if line.get("component") == "domain_mapper"
        and line.get("outcome") == "definitions_fallback"
    ]
    assert len(fallback_entries) == 1
    assert fallback_entries[0]["entity_id"] == result.role_node_ids["Manufacturer"]
    assert fallback_entries[0]["role_name"] == "Manufacturer"
    assert fallback_entries[0]["regulatory_instrument_id"] == _REGULATION_ID


def test_extract_roles_and_requirements_falls_back_for_unmatched_role_when_definitions_unit_exists(
    make_emitter: MakeEmitter, read_lines: ReadLines
) -> None:
    """AC-BI-009, end-to-end: a definitions unit defines "Manufacturer" only;
    duty units name both "Manufacturer" and "Importer" -> Manufacturer's
    DEFINES source_ref is the definitions unit's citation_ref, Importer's
    falls back to its own first duty unit_citation_ref, and exactly one
    definitions_fallback entry is emitted, naming "Importer" only.
    """
    emitter, log_path = make_emitter()
    native_graph = _FakeNativeGraph({"id": _REGULATION_ID})
    baseline_graph = _FakeBaselineGraph()
    definitions_unit = ExtractionUnit(
        citation_ref="Art. 2(1)",
        text="'Manufacturer' means any natural or legal person who develops products.",
        article_number="2",
        paragraph_number="1",
        article_heading="Definitions",
    )
    adapter = _FakeAdapter((definitions_unit, _UNIT_MANUFACTURER, _UNIT_IMPORTER))
    # FLAW-1: `definitions_unit`'s citation_ref is fed to BOTH passes (see
    # the earlier AC-BI-004 test's comment) -> compound-keyed fixture.
    call_completion = _scripted_call_completion_by_prompt_kind(
        {
            (definitions_unit.citation_ref, "definitions"): json.dumps({"terms": ["Manufacturer"]}),
            (definitions_unit.citation_ref, "duty"): json.dumps({"requirements": []}),
            (_UNIT_MANUFACTURER.citation_ref, "duty"): _requirements_json(
                role_name="Manufacturer", text="Conduct a cybersecurity risk assessment."
            ),
            (_UNIT_IMPORTER.citation_ref, "duty"): _requirements_json(
                role_name="Importer", text="Verify the manufacturer's conformity assessment."
            ),
        }
    )

    result = extract_roles_and_requirements(
        _REGULATION_ID,
        adapter=adapter,
        native_graph=native_graph,
        baseline_graph=baseline_graph,
        model="fake-model",
        call_completion=call_completion,
        emitter=emitter,
    )
    emitter.flush()

    defines_calls = _find_edge_call(baseline_graph, "DEFINES")
    source_refs_by_role_id = {
        call.params["target_id"]: call.params["source_ref"]
        for call in defines_calls
        if call.params is not None
    }
    assert (
        source_refs_by_role_id[result.role_node_ids["Manufacturer"]]
        == definitions_unit.citation_ref
    )
    assert source_refs_by_role_id[result.role_node_ids["Importer"]] == _UNIT_IMPORTER.citation_ref

    lines = read_lines(log_path)
    fallback_entries = [
        line
        for line in lines
        if line.get("component") == "domain_mapper"
        and line.get("outcome") == "definitions_fallback"
    ]
    assert len(fallback_entries) == 1
    assert fallback_entries[0]["role_name"] == "Importer"


def test_extract_roles_and_requirements_zero_definitions_units_issues_zero_extra_llm_calls(
    make_emitter: MakeEmitter,
) -> None:
    """AC-BI-007, end-to-end: only duty units present -> the total LLM call
    count equals exactly the duty-unit count (no extra call for an empty
    definitions pass).
    """
    emitter, _log_path = make_emitter()
    native_graph = _FakeNativeGraph({"id": _REGULATION_ID})
    baseline_graph = _FakeBaselineGraph()
    adapter = _FakeAdapter((_UNIT_MANUFACTURER, _UNIT_IMPORTER))
    call_count = 0
    scripted = _scripted_call_completion(
        {
            _UNIT_MANUFACTURER.citation_ref: _requirements_json(
                role_name="Manufacturer", text="Conduct a cybersecurity risk assessment."
            ),
            _UNIT_IMPORTER.citation_ref: _requirements_json(
                role_name="Importer", text="Verify the manufacturer's conformity assessment."
            ),
        }
    )

    def counting_call_completion(
        *, model: str, messages: list[dict[str, str]], timeout: float
    ) -> ModelResponse:
        nonlocal call_count
        call_count += 1
        return scripted(model=model, messages=messages, timeout=timeout)

    extract_roles_and_requirements(
        _REGULATION_ID,
        adapter=adapter,
        native_graph=native_graph,
        baseline_graph=baseline_graph,
        model="fake-model",
        call_completion=counting_call_completion,
        emitter=emitter,
    )

    assert call_count == 2


def test_extract_roles_and_requirements_pools_terms_across_multiple_definitions_units(
    make_emitter: MakeEmitter,
) -> None:
    """AC-BI-006, end-to-end: two definitions-headed units (simulating
    one-per-paragraph), one defining "Manufacturer", the other "Importer" ->
    both roles' DEFINES edges get their respective definitions-unit
    citation_refs, proving pooling merges across units at the full pipeline
    level.
    """
    emitter, _log_path = make_emitter()
    native_graph = _FakeNativeGraph({"id": _REGULATION_ID})
    baseline_graph = _FakeBaselineGraph()
    definitions_unit_manufacturer = ExtractionUnit(
        citation_ref="Art. 2(1)",
        text="'Manufacturer' means any natural or legal person who develops products.",
        article_number="2",
        paragraph_number="1",
        article_heading="Definitions",
    )
    definitions_unit_importer = ExtractionUnit(
        citation_ref="Art. 2(2)",
        text=(
            "'Importer' means any natural or legal person established in the Union who "
            "places on the market a product from a third country."
        ),
        article_number="2",
        paragraph_number="2",
        article_heading="Definitions",
    )
    adapter = _FakeAdapter(
        (
            definitions_unit_manufacturer,
            definitions_unit_importer,
            _UNIT_MANUFACTURER,
            _UNIT_IMPORTER,
        )
    )
    # FLAW-1: both definitions units' citation_refs are fed to BOTH passes
    # (see the earlier AC-BI-004 test's comment) -> compound-keyed fixture.
    call_completion = _scripted_call_completion_by_prompt_kind(
        {
            (definitions_unit_manufacturer.citation_ref, "definitions"): json.dumps(
                {"terms": ["Manufacturer"]}
            ),
            (definitions_unit_manufacturer.citation_ref, "duty"): json.dumps({"requirements": []}),
            (definitions_unit_importer.citation_ref, "definitions"): json.dumps(
                {"terms": ["Importer"]}
            ),
            (definitions_unit_importer.citation_ref, "duty"): json.dumps({"requirements": []}),
            (_UNIT_MANUFACTURER.citation_ref, "duty"): _requirements_json(
                role_name="Manufacturer", text="Conduct a cybersecurity risk assessment."
            ),
            (_UNIT_IMPORTER.citation_ref, "duty"): _requirements_json(
                role_name="Importer", text="Verify the manufacturer's conformity assessment."
            ),
        }
    )

    result = extract_roles_and_requirements(
        _REGULATION_ID,
        adapter=adapter,
        native_graph=native_graph,
        baseline_graph=baseline_graph,
        model="fake-model",
        call_completion=call_completion,
        emitter=emitter,
    )

    defines_calls = _find_edge_call(baseline_graph, "DEFINES")
    source_refs_by_role_id = {
        call.params["target_id"]: call.params["source_ref"]
        for call in defines_calls
        if call.params is not None
    }
    assert (
        source_refs_by_role_id[result.role_node_ids["Manufacturer"]]
        == definitions_unit_manufacturer.citation_ref
    )
    assert (
        source_refs_by_role_id[result.role_node_ids["Importer"]]
        == definitions_unit_importer.citation_ref
    )
