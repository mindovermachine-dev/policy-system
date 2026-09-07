"""Tests for ps_service.domain_mapper.governance.derive_governance_artifacts (issue #54, S3).

Per the binding testing convention (§0.3/§0.5, mirrored from `test_derivation.py`):
`call_completion` is faked with a hand-written structural fake satisfying
`CompletionCaller`'s Protocol, scripted per-call in Capability/Policy/Standard
processing order -- never `unittest.mock.Mock`/`MagicMock`. `baseline_graph`
is a hand-written structural fake satisfying the module's own `GraphHandle`
Protocol, mirroring `test_derivation.py`'s `_FakeBaselineGraph`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

import pytest
from litellm.types.utils import Choices, Message, ModelResponse

from ps_service.domain_mapper.errors import DomainMapperGovernanceError
from ps_service.domain_mapper.governance import derive_governance_artifacts
from ps_service.domain_mapper.identity import control_id, policy_id, standard_id

if TYPE_CHECKING:
    from domain_mapper._fakes import MakeEmitter, ReadLines
    from ps_service.llm_interface.client import CompletionCaller


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


def _scripted_sequential_call_completion(responses: list[str | Exception]) -> CompletionCaller:
    """A `CompletionCaller` fake returning `responses` in the exact order
    `derive_governance_artifacts` calls the LLM -- Capability-then-Policy-
    then-Standard-then-Control processing order. Raises if more calls are
    made than were scripted. Mirrors `test_derivation.py`'s own fake exactly.
    """
    remaining = list(responses)

    def _call(*, model: str, messages: list[dict[str, str]], timeout: float) -> ModelResponse:
        if not remaining:
            raise AssertionError("no more scripted responses -- unexpected extra LLM call")
        next_item = remaining.pop(0)
        if isinstance(next_item, Exception):
            raise next_item
        return _model_response(next_item)

    return _call


def _never_called_completion() -> CompletionCaller:
    """A `CompletionCaller` fake that raises if it is EVER invoked."""

    def _call(*, model: str, messages: list[dict[str, str]], timeout: float) -> ModelResponse:
        raise AssertionError("call_completion should never be invoked")

    return _call


def _policy_mint_response(new_title: str, confidence: float = 0.9) -> str:
    return json.dumps(
        {
            "matched_existing_id": None,
            "new_title": new_title,
            "unmatchable": False,
            "confidence": confidence,
        }
    )


def _policy_match_response(matched_existing_id: str, confidence: float = 0.85) -> str:
    return json.dumps(
        {
            "matched_existing_id": matched_existing_id,
            "new_title": None,
            "unmatchable": False,
            "confidence": confidence,
        }
    )


def _policy_unmatchable_response(confidence: float = 0.3) -> str:
    return json.dumps(
        {
            "matched_existing_id": None,
            "new_title": None,
            "unmatchable": True,
            "confidence": confidence,
        }
    )


def _standard_response(title: str, description: str | None = None, confidence: float = 0.9) -> str:
    return json.dumps({"title": title, "description": description, "confidence": confidence})


def _control_response(
    control_type: str, title: str, description: str | None = None, confidence: float = 0.9
) -> str:
    return json.dumps(
        {"type": control_type, "title": title, "description": description, "confidence": confidence}
    )


# --- fake baseline graph -----------------------------------------------------


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


class _FakeBaselineGraph:
    """Satisfies `governance.py`'s own `GraphHandle` Protocol structurally.

    Answers the `RETURN r.source_type` and `RETURN c.id, c.name` read
    queries with scripted rows; captures every other `(query, params)` call
    -- the write-side calls `persist_governance_graph` issues -- for
    assertion. Mirrors `test_derivation.py`'s `_FakeBaselineGraph` style.
    """

    def __init__(self, *, source_type: str | None, capability_rows: list[list[object]]) -> None:
        self._source_type = source_type
        self._capability_rows: list[object] = [*capability_rows]
        self.calls: list[_RecordedCall] = []

    def query(self, q: str, params: dict[str, object] | None = None) -> _FakeQueryResult:
        if "RETURN r.source_type" in q:
            rows: list[object] = [[self._source_type]] if self._source_type is not None else []
            return _FakeQueryResult(rows)
        if "RETURN c.id, c.name" in q:
            return _FakeQueryResult(self._capability_rows)
        self.calls.append(_RecordedCall(q, params))
        return _FakeQueryResult([[0]])


def _find_edge_calls(graph: _FakeBaselineGraph, relationship_type: str) -> list[_RecordedCall]:
    return [call for call in graph.calls if f"[:{relationship_type}]" in call.query]


def _find_node_calls(graph: _FakeBaselineGraph, label: str) -> list[_RecordedCall]:
    return [call for call in graph.calls if call.query.startswith(f"MERGE (n:{label} ")]


# --- test_derives_policy_standard_control_for_internal_source (AC-BI-005/006) ---


def test_derives_policy_standard_control_for_internal_source(make_emitter: MakeEmitter) -> None:
    """2 Capabilities: one mints a new Policy, the second matches it -- both
    still get a GOVERNED_BY edge, but only one Policy node is minted. That
    one Policy gets exactly one Standard, and that Standard gets exactly one
    Control. Every id asserted is self-consistent identity-function output
    (D-B3), never a hand-authored literal.
    """
    emitter, _log_path = make_emitter()
    graph = _FakeBaselineGraph(
        source_type="internal",
        capability_rows=[
            ["cap_security_logging_abc123", "Security Logging"],
            ["cap_access_control_def456", "Access Control"],
        ],
    )
    policy_title = "Data Protection Policy"
    expected_policy_id = policy_id(policy_title)
    standard_title = "Security Log Retention Standard"
    expected_standard_id = standard_id(expected_policy_id, "1")
    control_title = "Automated Log Retention Integrity Check"
    expected_control_id = control_id(expected_standard_id, "automated")

    call_completion = _scripted_sequential_call_completion(
        [
            _policy_mint_response(policy_title),
            _policy_match_response(expected_policy_id),
            _standard_response(standard_title),
            _control_response("automated", control_title),
        ]
    )

    result = derive_governance_artifacts(
        "ENGPRAC-3.0",
        baseline_graph=graph,
        model="fake-model",
        call_completion=call_completion,
        emitter=emitter,
    )

    assert result.regulatory_instrument_id == "ENGPRAC-3.0"
    assert result.policy_node_ids == (expected_policy_id,)
    assert result.standard_node_ids == (expected_standard_id,)
    assert result.control_node_ids == (expected_control_id,)
    assert result.unmatched_capability_ids == ()

    # Exactly one Policy node written, despite two GOVERNED_BY edges.
    policy_node_calls = _find_node_calls(graph, "Policy")
    assert len(policy_node_calls) == 1
    assert policy_node_calls[0].params == {
        "id": expected_policy_id,
        "properties": {"title": policy_title, "status": "draft", "confidence": 0.9},
    }

    governed_by_calls = _find_edge_calls(graph, "GOVERNED_BY")
    assert len(governed_by_calls) == 2
    governed_by_sources = {call.params["source_id"] for call in governed_by_calls if call.params}
    assert governed_by_sources == {"cap_security_logging_abc123", "cap_access_control_def456"}
    assert all(
        call.params["target_id"] == expected_policy_id for call in governed_by_calls if call.params
    )

    standard_node_calls = _find_node_calls(graph, "Standard")
    assert len(standard_node_calls) == 1
    assert standard_node_calls[0].params == {
        "id": expected_standard_id,
        "properties": {
            "title": standard_title,
            "implementation_status": "draft",
            "confidence": 0.9,
        },
    }
    supported_by_calls = _find_edge_calls(graph, "SUPPORTED_BY")
    assert len(supported_by_calls) == 1
    assert supported_by_calls[0].params == {
        "source_id": expected_policy_id,
        "target_id": expected_standard_id,
    }

    control_node_calls = _find_node_calls(graph, "Control")
    assert len(control_node_calls) == 1
    assert control_node_calls[0].params == {
        "id": expected_control_id,
        "properties": {
            "type": "automated",
            "title": control_title,
            "implementation_status": "planned",
            "confidence": 0.9,
        },
    }
    implemented_by_calls = _find_edge_calls(graph, "IMPLEMENTED_BY")
    assert len(implemented_by_calls) == 1
    assert implemented_by_calls[0].params == {
        "source_id": expected_standard_id,
        "target_id": expected_control_id,
    }


# --- test_raises_for_source_type_external (AC-BI-008) -----------------------


def test_raises_for_source_type_external(make_emitter: MakeEmitter) -> None:
    """Defense-in-depth: even if invoked directly against an external
    RegulatoryInstrument's baseline graph, `derive_governance_artifacts`
    refuses to run -- no Capability is read, no LLM call is ever made.
    """
    emitter, _log_path = make_emitter()
    graph = _FakeBaselineGraph(
        source_type="external",
        capability_rows=[["cap_security_logging_abc123", "Security Logging"]],
    )

    with pytest.raises(DomainMapperGovernanceError) as exc_info:
        derive_governance_artifacts(
            "CRA-1.0",
            baseline_graph=graph,
            model="fake-model",
            call_completion=_never_called_completion(),
            emitter=emitter,
        )

    assert "CRA-1.0" in str(exc_info.value)
    assert "external" in str(exc_info.value)
    assert graph.calls == []


def test_raises_for_missing_regulatory_instrument(make_emitter: MakeEmitter) -> None:
    """A RegulatoryInstrument row that doesn't resolve at all is treated the
    same as a non-internal source_type -- never a crash, never silently
    proceeding with `source_type=None`.
    """
    emitter, _log_path = make_emitter()
    graph = _FakeBaselineGraph(source_type=None, capability_rows=[])

    with pytest.raises(DomainMapperGovernanceError):
        derive_governance_artifacts(
            "MISSING-1.0",
            baseline_graph=graph,
            model="fake-model",
            call_completion=_never_called_completion(),
            emitter=emitter,
        )


# --- test_unmatched_capability_surfaced_not_skipped (AC-BI-014) -------------


def test_unmatched_capability_surfaced_not_skipped(
    make_emitter: MakeEmitter, read_lines: ReadLines
) -> None:
    """2 Capabilities: the first's Policy derivation is unmatchable, the
    second mints normally. The first is surfaced in
    `unmatched_capability_ids`, has no GOVERNED_BY edge, an
    `outcome="unmatched"` log entry is emitted naming it, and derivation
    continues for the second Capability -- it still gets its own Policy/
    Standard/Control.
    """
    emitter, log_path = make_emitter()
    graph = _FakeBaselineGraph(
        source_type="internal",
        capability_rows=[
            ["cap_too_vague_xyz789", "Some Vague Capability"],
            ["cap_access_control_def456", "Access Control"],
        ],
    )
    policy_title = "Access Governance Policy"
    call_completion = _scripted_sequential_call_completion(
        [
            _policy_unmatchable_response(),
            _policy_mint_response(policy_title),
            _standard_response("Access Governance Standard"),
            _control_response("manual", "Quarterly Access Review"),
        ]
    )

    result = derive_governance_artifacts(
        "ENGPRAC-3.0",
        baseline_graph=graph,
        model="fake-model",
        call_completion=call_completion,
        emitter=emitter,
    )
    emitter.flush()

    assert result.unmatched_capability_ids == ("cap_too_vague_xyz789",)
    assert len(result.policy_node_ids) == 1

    governed_by_calls = _find_edge_calls(graph, "GOVERNED_BY")
    governed_by_sources = {call.params["source_id"] for call in governed_by_calls if call.params}
    assert "cap_too_vague_xyz789" not in governed_by_sources
    assert governed_by_sources == {"cap_access_control_def456"}

    lines = read_lines(log_path)
    unmatched_entries = [line for line in lines if line.get("outcome") == "unmatched"]
    assert len(unmatched_entries) == 1
    assert unmatched_entries[0]["entity_id"] == "cap_too_vague_xyz789"


def test_unmatched_capability_from_malformed_llm_response_also_surfaced(
    make_emitter: MakeEmitter,
) -> None:
    """A malformed (unparseable) Policy-derivation response is isolated the
    same way an explicit `unmatchable: true` is -- surfaced, never an
    uncaught exception aborting the whole run.
    """
    emitter, _log_path = make_emitter()
    graph = _FakeBaselineGraph(
        source_type="internal",
        capability_rows=[["cap_security_logging_abc123", "Security Logging"]],
    )
    call_completion = _scripted_sequential_call_completion(["{not valid json"])

    result = derive_governance_artifacts(
        "ENGPRAC-3.0",
        baseline_graph=graph,
        model="fake-model",
        call_completion=call_completion,
        emitter=emitter,
    )

    assert result.unmatched_capability_ids == ("cap_security_logging_abc123",)
    assert result.policy_node_ids == ()
