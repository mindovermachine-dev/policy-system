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

import httpx
import openai
import pytest
from litellm.types.utils import Choices, Message, ModelResponse

from ps_service.domain_mapper.errors import DomainMapperExtractionError
from ps_service.domain_mapper.extraction import (
    _build_requirement_graph,
    _canonicalize_roles,
    _extract_candidates_for_unit,
    extract_roles_and_requirements,
)
from ps_service.domain_mapper.falkordb_client import GraphHandle
from ps_service.domain_mapper.models import ExtractionUnit, RequirementCandidate
from ps_service.llm_interface.client import CompletionCaller
from ps_service.llm_interface.errors import LlmProviderError
from ps_service.logging import bind_run_context

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
    make_emitter,
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


def test_extract_candidates_for_unit_sends_system_and_user_messages(make_emitter) -> None:
    """The system prompt and the unit's own text are sent as two distinct
    messages, with the unit's text delimited (not concatenated into the
    system prompt) — L2's untrusted-content rule."""
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


def test_extract_candidates_for_unit_propagates_llm_provider_error_unchanged(make_emitter) -> None:
    """An infra failure calling the LLM at all (route_completion's own
    LlmProviderError) is not caught here — it propagates unchanged, the
    infra-vs-content failure split PLAN_REVIEWED.md §5.2 requires."""
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
    make_emitter,
) -> None:
    """A malformed/unparseable LLM *response* (as opposed to an infra
    failure) surfaces as DomainMapperExtractionError, naming the unit —
    this function does not swallow it; per-unit failure isolation is the
    orchestrating extract_roles_and_requirements's job, not this one's."""
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
    second's."""
    first = _candidate(unit_citation_ref="Art. 13(1)", role_name="Manufacturer")
    second = _candidate(unit_citation_ref="Art. 14(2)", role_name="Manufacturer")

    role_nodes, role_edges, role_node_ids = _canonicalize_roles([first, second], _REGULATION_ID)

    assert len(role_nodes) == 1
    assert len(role_edges) == 1
    assert role_edges[0].source_ref == "Art. 13(1)"
    assert role_edges[0].role_node_id == role_nodes[0].id
    assert role_node_ids == {"Manufacturer": role_nodes[0].id}


def test_canonicalize_roles_produces_distinct_nodes_for_distinct_role_names() -> None:
    manufacturer = _candidate(role_name="Manufacturer")
    importer = _candidate(role_name="Importer")

    role_nodes, role_edges, role_node_ids = _canonicalize_roles(
        [manufacturer, importer], _REGULATION_ID
    )

    assert len(role_nodes) == 2
    assert len(role_edges) == 2
    assert role_node_ids["Manufacturer"] != role_node_ids["Importer"]


def test_canonicalize_roles_stamps_first_candidates_confidence() -> None:
    first = _candidate(role_name="Manufacturer", confidence=0.4)
    second = _candidate(role_name="Manufacturer", confidence=0.95)

    role_nodes, _role_edges, _role_node_ids = _canonicalize_roles(
        [first, second], _REGULATION_ID
    )

    assert role_nodes[0].properties["confidence"] == 0.4


# --- _build_requirement_graph (Increment 8, B2 fix) ------------------------


def test_build_requirement_graph_deduplicates_identical_id_and_text() -> None:
    """A trivial same-input sanity check (two candidates that happen to
    compute the same id and carry byte-identical text within one call) —
    NOT proof of cross-call idempotent re-extraction, which is out of this
    test's scope."""
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
    disambiguated id."""
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
    a two-way case."""
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


class _FakeRegulationNode:
    """Satisfies extraction.py's own `_RegulationNode` Protocol structurally
    — only `.properties` is ever read."""

    def __init__(self, properties: dict[str, object]) -> None:
        self.properties = properties


class _FakeNativeGraph:
    """Satisfies `GraphHandle` structurally. Answers
    `MATCH (r:Regulation) RETURN r` with a scripted Regulation node;
    ignores any other query (the fake adapter never actually queries it in
    these tests)."""

    def __init__(self, regulation_properties: dict[str, object]) -> None:
        self._regulation_properties = regulation_properties

    def query(self, q: str, params: dict[str, object] | None = None) -> _FakeQueryResult:
        return _FakeQueryResult([[_FakeRegulationNode(self._regulation_properties)]])


class _FakeBaselineGraph:
    """Satisfies `GraphHandle` structurally, capturing every `(query,
    params)` call for assertion — mirrors `test_graph_writer.py`'s
    `_FakeGraph`."""

    def __init__(self) -> None:
        self.calls: list[_RecordedCall] = []

    def query(self, q: str, params: dict[str, object] | None = None) -> _FakeQueryResult:
        self.calls.append(_RecordedCall(q, params))
        return _FakeQueryResult([[0]])


class _FakeAdapter:
    """Satisfies `DomainMappingAdapter` structurally — returns a
    pre-scripted tuple of `ExtractionUnit`s regardless of `graph`."""

    def __init__(self, units: tuple[ExtractionUnit, ...]) -> None:
        self._units = units

    def read_native_units(self, graph: GraphHandle) -> tuple[ExtractionUnit, ...]:
        return self._units


def _scripted_call_completion(responses: dict[str, str | Exception]) -> CompletionCaller:
    """A `CompletionCaller` fake keyed by the unit's `citation_ref`, which
    `_build_extraction_messages` always embeds in the user message as
    `"Citation: {citation_ref}"` — lets a test script one canned response
    (or an exception to raise) per unit regardless of call order."""

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
    make_emitter,
) -> None:
    """AC-001: two units, two different roles -> Role/Requirement nodes with
    DEFINES/EXPRESSES edges carrying source_ref == unit.citation_ref."""
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
    make_emitter,
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
    properties = cast_dict(requirement_calls[0].params["properties"])
    assert properties["confidence"] == 0.1


def cast_dict(value: object) -> dict[str, object]:
    assert isinstance(value, dict)
    return value


def test_extract_roles_and_requirements_ac006_emits_log_entry_with_bound_run_id(
    make_emitter, read_lines
) -> None:
    """AC-006: with a run context bound, the emitted outcome="succeeded"
    entry carries the bound run_id — mirrors
    test_route_completion_logs_run_id.py exactly."""
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
    make_emitter, read_lines
) -> None:
    """One of two units' scripted response is malformed JSON -> the OTHER
    unit's candidates still persist, an outcome="error" entry is emitted
    for the bad unit's citation_ref, and skipped_unit_count == 1."""
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


def test_extract_roles_and_requirements_propagates_llm_provider_error_and_aborts(
    make_emitter,
) -> None:
    """An LlmProviderError from one unit's call propagates and aborts the
    whole call — infra failures are not per-unit-isolated (no partial-write
    guarantee claimed either way)."""
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
    make_emitter,
) -> None:
    """Q2 fix: an adapter returning zero units is not an error — the
    Regulation node is still MERGEd, and a well-formed all-zero
    ExtractionResult is returned."""
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
    assert baseline_graph.calls[0].query == "MERGE (n:Regulation {id: $id}) SET n += $properties"


def test_extract_roles_and_requirements_surfaces_collision_without_aborting(
    make_emitter, read_lines
) -> None:
    """B2 fix: two units whose candidates land on the same requirement_id
    with different text -> both Requirement nodes persisted (one
    #2-suffixed), requirement_id_collisions is non-empty, an
    outcome="collision" entry is emitted naming the base id — no exception,
    no aborted call."""
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
