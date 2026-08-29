"""AC-005 proof: per-regulation baseline graph isolation (PLAN_REVIEWED.md
§11 Increment 17).

Two parts, per the plan:

(a) Unit-level, hand-written structural fakes (`_FakeBaselineGraph`/
`_FakeDerivationBaselineGraph`, mirroring `test_extraction.py`'s and
`test_derivation.py`'s own fakes exactly -- no `unittest.mock`, per the
binding testing convention, PLAN_REVIEWED.md §0.3/§0.5). Two INDEPENDENT
`GraphHandle` fakes stand in for `cra_baseline`/`gdpr_baseline`; each is
passed to its own `extract_roles_and_requirements`/
`derive_obligations_and_capabilities` call. This proves the abstraction
itself -- not just "two Python objects are different objects" (trivially
true) -- by asserting a node/edge id computed for one regulation's run
NEVER appears anywhere (query string or params) in the OTHER graph's
captured call log. If any code under test carried a hardcoded graph name,
a module-level cache, or any other shared state, cross-contamination would
show up here as a shared id or shared literal string leaking across the
two independent fakes.

(b) `@pytest.mark.falkordb_live`: the same proof against a real, reachable
FalkorDB instance (127.0.0.1:6379), using two graph names suffixed
`_isolation_test` so this test never collides with or pollutes
`cra_baseline`/`gdpr_baseline` or any other graph name in real use. Both
test graphs are deleted in a `finally` block regardless of assertion
outcome, so no live residue survives a run of this test, including a
failing one.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

import pytest
from litellm.types.utils import Choices, Message, ModelResponse

from ps_service.domain_mapper.derivation import derive_obligations_and_capabilities
from ps_service.domain_mapper.extraction import extract_roles_and_requirements
from ps_service.domain_mapper.falkordb_client import (
    GraphHandle,
    connect,
    select_graph,
)
from ps_service.domain_mapper.identity import obligation_id, role_id
from ps_service.domain_mapper.models import ExtractionUnit

if TYPE_CHECKING:
    from domain_mapper._fakes import MakeEmitter
    from ps_service.llm_interface.client import CompletionCaller

# --- shared helpers ----------------------------------------------------


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


def _requirements_json(*, role_name: str, text: str, confidence: float = 0.9) -> str:
    return json.dumps(
        {
            "requirements": [
                {
                    "role_name": role_name,
                    "text": text,
                    "type": "requirement",
                    "letter_suffix": None,
                    "confidence": confidence,
                }
            ]
        }
    )


@dataclass
class _RecordedCall:
    query: str
    params: dict[str, object] | None


class _HasCallLog(Protocol):
    """Structural type shared by both fake baseline graphs: a captured `(query, params)` log."""

    calls: list[_RecordedCall]


class _FakeQueryResult:
    """Satisfies `GraphQueryResult` structurally."""

    def __init__(self, result_set: list[object]) -> None:
        self._result_set = result_set

    @property
    def result_set(self) -> list[object]:
        return self._result_set


class _FakeRegulatoryInstrumentNode:
    """Satisfies `extraction.py`'s own `_RegulatoryInstrumentNode` Protocol
    structurally -- only `.properties` is ever read.
    """

    def __init__(self, properties: dict[str, object]) -> None:
        self.properties = properties


class _FakeNativeGraph:
    """Satisfies `GraphHandle` structurally. Answers
    `MATCH (r:RegulatoryInstrument) RETURN r` with a scripted Regulation node --
    mirrors `test_extraction.py`'s own `_FakeNativeGraph`.
    """

    def __init__(self, regulatory_instrument_properties: dict[str, object]) -> None:
        self._regulatory_instrument_properties = regulatory_instrument_properties

    def query(self, q: str, params: dict[str, object] | None = None) -> _FakeQueryResult:
        return _FakeQueryResult(
            [[_FakeRegulatoryInstrumentNode(self._regulatory_instrument_properties)]]
        )


class _FakeBaselineGraph:
    """Satisfies `GraphHandle` structurally, capturing every `(query,
    params)` call -- mirrors `test_extraction.py`'s `_FakeBaselineGraph`.
    Each instance is a wholly independent, isolated in-memory call log:
    there is no class-level or module-level state shared between two
    instances of this fake, which is exactly the property this test is
    checking the PRODUCTION code (not this fake) actually respects.
    """

    def __init__(self) -> None:
        self.calls: list[_RecordedCall] = []

    def query(self, q: str, params: dict[str, object] | None = None) -> _FakeQueryResult:
        self.calls.append(_RecordedCall(q, params))
        return _FakeQueryResult([[0]])


class _FakeAdapter:
    """Satisfies `DomainMappingAdapter` structurally -- returns a
    pre-scripted tuple of `ExtractionUnit`s regardless of `graph`.
    """

    def __init__(self, units: tuple[ExtractionUnit, ...]) -> None:
        self._units = units

    def read_native_units(self, graph: GraphHandle) -> tuple[ExtractionUnit, ...]:
        return self._units


def _scripted_call_completion(responses: dict[str, str]) -> CompletionCaller:
    """A `CompletionCaller` fake keyed by the unit's `citation_ref`, mirrors
    `test_extraction.py`'s `_scripted_call_completion`.
    """

    def _call(*, model: str, messages: list[dict[str, str]], timeout: float) -> ModelResponse:
        user_content = messages[1]["content"]
        for citation_ref, response in responses.items():
            if f"Citation: {citation_ref}" in user_content:
                return _model_response(response)
        raise AssertionError(f"no scripted response for message: {user_content!r}")

    return _call


def _graph_mentions(graph: _HasCallLog, needle: str) -> bool:
    """True if `needle` appears anywhere in this graph's captured call
    log -- either in a query string or anywhere inside a call's params
    (stringified, so it also catches a needle nested inside a properties
    dict). This is the actual isolation assertion: a value written to one
    `GraphHandle` must never be observable, in any form, on a completely
    separate `GraphHandle` instance.
    """
    for call in graph.calls:
        if needle in call.query:
            return True
        if call.params is not None and needle in str(call.params):
            return True
    return False


# --- (a) unit-level: ExtractRolesAndRequirements ----------------------


def test_extract_roles_and_requirements_never_leaks_across_two_independent_graph_handles(
    make_emitter: MakeEmitter,
) -> None:
    emitter, _log_path = make_emitter()

    cra_graph = _FakeBaselineGraph()
    gdpr_graph = _FakeBaselineGraph()

    cra_unit = ExtractionUnit(
        citation_ref="Art. 13(1)",
        text="The manufacturer shall conduct a cybersecurity risk assessment.",
        article_number="13",
        paragraph_number="1",
        article_heading="Obligations of manufacturers",
    )
    gdpr_unit = ExtractionUnit(
        citation_ref="Art. 24(1)",
        text="The controller shall implement appropriate technical measures.",
        article_number="24",
        paragraph_number="1",
        article_heading="Responsibility of the controller",
    )

    cra_result = extract_roles_and_requirements(
        "CRA-1.0",
        adapter=_FakeAdapter((cra_unit,)),
        native_graph=_FakeNativeGraph({"id": "CRA-1.0"}),
        baseline_graph=cra_graph,
        model="fake-model",
        call_completion=_scripted_call_completion(
            {
                cra_unit.citation_ref: _requirements_json(
                    role_name="Manufacturer",
                    text="Conduct a cybersecurity risk assessment.",
                )
            }
        ),
        emitter=emitter,
    )
    gdpr_result = extract_roles_and_requirements(
        "GDPR-1.0",
        adapter=_FakeAdapter((gdpr_unit,)),
        native_graph=_FakeNativeGraph({"id": "GDPR-1.0"}),
        baseline_graph=gdpr_graph,
        model="fake-model",
        call_completion=_scripted_call_completion(
            {
                gdpr_unit.citation_ref: _requirements_json(
                    role_name="Controller",
                    text="Implement appropriate technical measures.",
                )
            }
        ),
        emitter=emitter,
    )

    cra_role_id = role_id("Manufacturer", "CRA-1.0")
    gdpr_role_id = role_id("Controller", "GDPR-1.0")
    assert cra_role_id in cra_result.role_node_ids.values()
    assert gdpr_role_id in gdpr_result.role_node_ids.values()
    (cra_requirement_id,) = cra_result.requirement_ids
    (gdpr_requirement_id,) = gdpr_result.requirement_ids

    # Sanity: each graph's own writes actually landed.
    assert _graph_mentions(cra_graph, cra_role_id)
    assert _graph_mentions(cra_graph, cra_requirement_id)
    assert _graph_mentions(gdpr_graph, gdpr_role_id)
    assert _graph_mentions(gdpr_graph, gdpr_requirement_id)

    # The actual isolation proof: nothing written for one regulation's run
    # is observable, in any form, on the other regulation's GraphHandle.
    assert not _graph_mentions(gdpr_graph, cra_role_id)
    assert not _graph_mentions(gdpr_graph, cra_requirement_id)
    assert not _graph_mentions(gdpr_graph, "CRA-1.0")
    assert not _graph_mentions(gdpr_graph, "Manufacturer")
    assert not _graph_mentions(cra_graph, gdpr_role_id)
    assert not _graph_mentions(cra_graph, gdpr_requirement_id)
    assert not _graph_mentions(cra_graph, "GDPR-1.0")
    assert not _graph_mentions(cra_graph, "Controller")


# --- (a) unit-level: DeriveObligationsAndCapabilities -------------------


class _FakeDerivationBaselineGraph:
    """Satisfies `GraphHandle` structurally. Answers
    `_read_requirements_by_role`'s `OPTIONAL MATCH (rl:Role...` query with a
    scripted row set; captures every other `(query, params)` call --
    mirrors `test_derivation.py`'s own `_FakeBaselineGraph` exactly.
    """

    def __init__(self, requirement_rows: list[list[object]]) -> None:
        self._requirement_rows: list[object] = [*requirement_rows]
        self.calls: list[_RecordedCall] = []

    def query(self, q: str, params: dict[str, object] | None = None) -> _FakeQueryResult:
        if "OPTIONAL MATCH (rl:Role" in q:
            return _FakeQueryResult(self._requirement_rows)
        self.calls.append(_RecordedCall(q, params))
        return _FakeQueryResult([[0]])


def _mint_response(new_text: str, confidence: float = 0.9) -> str:
    return json.dumps(
        {
            "matched_existing_id": None,
            "new_text": new_text,
            "unmatchable": False,
            "confidence": confidence,
        }
    )


def _capability_mint_response(new_name: str, confidence: float = 0.85) -> str:
    return json.dumps(
        {
            "capabilities": [
                {
                    "matched_existing_id": None,
                    "new_name": new_name,
                    "new_description": None,
                    "confidence": confidence,
                }
            ]
        }
    )


def _scripted_sequential_call_completion(responses: list[str]) -> CompletionCaller:
    remaining = list(responses)

    def _call(*, model: str, messages: list[dict[str, str]], timeout: float) -> ModelResponse:
        if not remaining:
            raise AssertionError("no more scripted responses -- unexpected extra LLM call")
        return _model_response(remaining.pop(0))

    return _call


def test_derive_obligations_and_capabilities_never_leaks_across_two_independent_graph_handles(
    make_emitter: MakeEmitter,
) -> None:
    emitter, _log_path = make_emitter()

    cra_role = "role_manufacturer_cra"
    gdpr_role = "role_controller_gdpr"
    cra_graph = _FakeDerivationBaselineGraph(
        [
            [
                "CRA_req_art_13.1",
                "Conduct a cybersecurity risk assessment.",
                cra_role,
                cra_role,
                "Manufacturer",
            ]
        ]
    )
    gdpr_graph = _FakeDerivationBaselineGraph(
        [
            [
                "GDPR_req_art_24.1",
                "Implement appropriate technical measures.",
                gdpr_role,
                gdpr_role,
                "Controller",
            ]
        ]
    )

    cra_mint_text = "Conduct Cybersecurity Risk Assessment"
    gdpr_mint_text = "Implement Appropriate Technical Measures"

    cra_result = derive_obligations_and_capabilities(
        "CRA-1.0",
        baseline_graph=cra_graph,
        model="fake-model",
        call_completion=_scripted_sequential_call_completion(
            [_mint_response(cra_mint_text), _capability_mint_response("Risk Assessment Tooling")]
        ),
        emitter=emitter,
    )
    gdpr_result = derive_obligations_and_capabilities(
        "GDPR-1.0",
        baseline_graph=gdpr_graph,
        model="fake-model",
        call_completion=_scripted_sequential_call_completion(
            [
                _mint_response(gdpr_mint_text),
                _capability_mint_response("Technical Measures Registry"),
            ]
        ),
        emitter=emitter,
    )

    cra_obligation_id = obligation_id(cra_role, cra_mint_text)
    gdpr_obligation_id = obligation_id(gdpr_role, gdpr_mint_text)
    assert cra_result.obligation_node_ids == (cra_obligation_id,)
    assert gdpr_result.obligation_node_ids == (gdpr_obligation_id,)

    # Sanity: each graph's own writes actually landed.
    assert _graph_mentions(cra_graph, cra_obligation_id)
    assert _graph_mentions(gdpr_graph, gdpr_obligation_id)

    # The actual isolation proof.
    assert not _graph_mentions(gdpr_graph, cra_obligation_id)
    assert not _graph_mentions(gdpr_graph, cra_role)
    assert not _graph_mentions(gdpr_graph, "CRA_req_art_13.1")
    assert not _graph_mentions(cra_graph, gdpr_obligation_id)
    assert not _graph_mentions(cra_graph, gdpr_role)
    assert not _graph_mentions(cra_graph, "GDPR_req_art_24.1")


# --- (b) live: two real, distinct FalkorDB graphs -----------------------

_CRA_TEST_GRAPH = "cra_baseline_isolation_test"
_GDPR_TEST_GRAPH = "gdpr_baseline_isolation_test"


@pytest.mark.falkordb_live
def test_baseline_graph_isolation_against_real_falkordb() -> None:
    """Real connect to 127.0.0.1:6379. Writes a distinguishable node into
    each of two distinct, test-specific graph names, queries both back,
    asserts genuine isolation, then deletes both graphs -- in a `finally`
    block, so no live residue survives even a failing run.
    """
    db = connect(host="127.0.0.1", port=6379)

    # Defensive pre-clean, in case a prior failed run left residue.
    existing = set(db.list_graphs())
    for name in (_CRA_TEST_GRAPH, _GDPR_TEST_GRAPH):
        if name in existing:
            db.select_graph(name).delete()

    try:
        cra_graph = select_graph(db, _CRA_TEST_GRAPH)
        gdpr_graph = select_graph(db, _GDPR_TEST_GRAPH)

        cra_graph.query(
            "MERGE (n:Role {id: $id}) SET n += $properties",
            params={"id": "cra_isolation_marker", "properties": {"name": "CRA_MARKER"}},
        )
        gdpr_graph.query(
            "MERGE (n:Role {id: $id}) SET n += $properties",
            params={"id": "gdpr_isolation_marker", "properties": {"name": "GDPR_MARKER"}},
        )

        # Sanity: each graph's own write is genuinely there.
        cra_own = cra_graph.query(
            "MATCH (n:Role {id: $id}) RETURN n", params={"id": "cra_isolation_marker"}
        )
        gdpr_own = gdpr_graph.query(
            "MATCH (n:Role {id: $id}) RETURN n", params={"id": "gdpr_isolation_marker"}
        )
        assert len(cra_own.result_set) == 1
        assert len(gdpr_own.result_set) == 1

        # The actual isolation proof: a node written into one graph is
        # never visible when queried against the other, distinct graph.
        cra_queried_for_gdpr_marker = cra_graph.query(
            "MATCH (n:Role {id: $id}) RETURN n", params={"id": "gdpr_isolation_marker"}
        )
        gdpr_queried_for_cra_marker = gdpr_graph.query(
            "MATCH (n:Role {id: $id}) RETURN n", params={"id": "cra_isolation_marker"}
        )
        assert len(cra_queried_for_gdpr_marker.result_set) == 0
        assert len(gdpr_queried_for_cra_marker.result_set) == 0
    finally:
        db.select_graph(_CRA_TEST_GRAPH).delete()
        db.select_graph(_GDPR_TEST_GRAPH).delete()
        remaining = set(db.list_graphs())
        assert _CRA_TEST_GRAPH not in remaining
        assert _GDPR_TEST_GRAPH not in remaining
