"""Tests for ps_service.domain_mapper.derivation._derive_obligations.

Per PLAN_REVIEWED.md §11 Increment 12 / the binding testing convention
(§0.3/§0.5): `call_completion` is faked with a hand-written structural fake
satisfying `CompletionCaller`'s Protocol, scripted per-call in Role-then-
Requirement document order — never `unittest.mock.Mock`/`MagicMock`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import pytest
from litellm.types.utils import Choices, Message, ModelResponse
from ps_service.domain_mapper.derivation import (
    _derive_capabilities,
    _derive_obligations,
    derive_obligations_and_capabilities,
)
from ps_service.domain_mapper.errors import DomainMapperDerivationError
from ps_service.domain_mapper.identity import capability_id, obligation_id
from ps_service.domain_mapper.models import ObligationNode, RoleRequirements
from ps_service.llm_interface.client import CompletionCaller
from ps_service.logging import bind_run_context


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


def _scripted_sequential_call_completion(responses: list[str]) -> CompletionCaller:
    """A `CompletionCaller` fake that returns `responses` in the exact
    order `_derive_obligations` calls the LLM — Role then Requirement
    document order (PLAN_REVIEWED.md §7.3). Raises if more calls are made
    than were scripted, so an unexpected extra call fails loudly rather
    than silently reusing a stale response."""
    remaining = list(responses)

    def _call(*, model: str, messages: list[dict[str, str]], timeout: float) -> ModelResponse:
        if not remaining:
            raise AssertionError("no more scripted responses -- unexpected extra LLM call")
        return _model_response(remaining.pop(0))

    return _call


def _match_response(matched_existing_id: str, confidence: float = 0.9) -> str:
    return json.dumps(
        {
            "matched_existing_id": matched_existing_id,
            "new_text": None,
            "unmatchable": False,
            "confidence": confidence,
        }
    )


def _mint_response(new_text: str, confidence: float = 0.9) -> str:
    return json.dumps(
        {
            "matched_existing_id": None,
            "new_text": new_text,
            "unmatchable": False,
            "confidence": confidence,
        }
    )


def _unmatchable_response(confidence: float = 0.4) -> str:
    return json.dumps(
        {
            "matched_existing_id": None,
            "new_text": None,
            "unmatchable": True,
            "confidence": confidence,
        }
    )


_ROLE_MANUFACTURER = "role_manufacturer_abc123"
_ROLE_IMPORTER = "role_importer_def456"


def test_derive_obligations_second_requirement_matches_first_minted_entry(make_emitter) -> None:
    """(a) Single Role, first Requirement mints (registry empty), second
    (matching-duty) Requirement matches the registry entry call 1 minted."""
    emitter, _log_path = make_emitter()
    role = RoleRequirements(
        role_node_id=_ROLE_MANUFACTURER,
        role_name="Manufacturer",
        requirements=(
            ("CRA_req_art_13.1", "Conduct a cybersecurity risk assessment."),
            ("CRA_req_art_13.2", "Keep the risk assessment documented and updated."),
        ),
    )
    minted_text = "Conduct Cybersecurity Risk Assessment"
    minted_id = obligation_id(minted_text)
    call_completion = _scripted_sequential_call_completion(
        [_mint_response(minted_text), _match_response(minted_id)]
    )

    obligation_nodes, has_edges, satisfied_by_edges, unmatched = _derive_obligations(
        (role,), model="fake-model", call_completion=call_completion, emitter=emitter
    )

    assert len(obligation_nodes) == 1
    assert obligation_nodes[0].id == minted_id
    assert obligation_nodes[0].properties["text"] == minted_text
    assert len(has_edges) == 1
    assert has_edges[0].role_node_id == _ROLE_MANUFACTURER
    assert has_edges[0].obligation_node_id == minted_id
    assert len(satisfied_by_edges) == 2
    assert {e.requirement_id for e in satisfied_by_edges} == {
        "CRA_req_art_13.1",
        "CRA_req_art_13.2",
    }
    assert all(e.obligation_node_id == minted_id for e in satisfied_by_edges)
    assert unmatched == ()


def test_derive_obligations_unmatchable_response_produces_no_obligation_and_no_exception(
    make_emitter,
) -> None:
    """(b) An unmatchable response produces obligation_node_id=None with no
    exception -- no Obligation is created, the Requirement is surfaced in
    unmatched_requirement_ids instead."""
    emitter, _log_path = make_emitter()
    role = RoleRequirements(
        role_node_id=_ROLE_MANUFACTURER,
        role_name="Manufacturer",
        requirements=(("CRA_req_art_13.9", "Some vague, unclear text."),),
    )
    call_completion = _scripted_sequential_call_completion([_unmatchable_response()])

    obligation_nodes, has_edges, satisfied_by_edges, unmatched = _derive_obligations(
        (role,), model="fake-model", call_completion=call_completion, emitter=emitter
    )

    assert obligation_nodes == ()
    assert has_edges == ()
    assert satisfied_by_edges == ()
    assert unmatched == ("CRA_req_art_13.9",)


def test_derive_obligations_same_role_convergence_on_independent_identical_mints(
    make_emitter,
) -> None:
    """(c) Two Requirements under the SAME Role independently produce
    identical mint text (two separate LLM calls, both minting, not
    matching) -> a single Obligation node results because the second
    call's obligation_id() collides with the registry entry the FIRST call
    created for the SAME role, so it's reused, not re-minted. One HAS
    edge, both Requirements SATISFIED_BY it, no role-qualification."""
    emitter, _log_path = make_emitter()
    role = RoleRequirements(
        role_node_id=_ROLE_MANUFACTURER,
        role_name="Manufacturer",
        requirements=(
            ("CRA_req_art_13.22a", "Cooperate with the market surveillance authority."),
            ("CRA_req_art_19.7", "Cooperate with market surveillance authority requests."),
        ),
    )
    duty_text = "Cooperate with Market Surveillance Authority Requests"
    # Both calls MINT -- the LLM is not shown a well-behaved match here on
    # purpose, proving the CODE-level registry, not the model, guarantees
    # convergence.
    call_completion = _scripted_sequential_call_completion(
        [_mint_response(duty_text), _mint_response(duty_text)]
    )

    obligation_nodes, has_edges, satisfied_by_edges, unmatched = _derive_obligations(
        (role,), model="fake-model", call_completion=call_completion, emitter=emitter
    )

    assert len(obligation_nodes) == 1
    assert obligation_nodes[0].properties["text"] == duty_text
    assert obligation_nodes[0].id == obligation_id(duty_text)
    assert len(has_edges) == 1
    assert has_edges[0].role_node_id == _ROLE_MANUFACTURER
    assert len(satisfied_by_edges) == 2
    assert {e.obligation_node_id for e in satisfied_by_edges} == {obligation_nodes[0].id}
    assert unmatched == ()


def test_derive_obligations_cross_role_collision_role_qualifies_second_roles_text(
    make_emitter,
) -> None:
    """(d) THE critical test: Role A's Requirement mints "Cooperate with
    Market Surveillance Authority Requests" (registered, one HAS edge to
    Role A). Role B is processed LATER IN THE SAME RUN; its Requirement
    independently mints the IDENTICAL text. This must be detected as a
    collision against Role A's already-registered Obligation and
    role-qualified for Role B -- Role A's own node/text/edge stay
    completely untouched. Direct proof of PLAN_REVIEWED.md §3.1/§7.3's B1
    fix: collision-aware, duty-text-only hashing -- NOT the rejected
    role-baked-hash design that would mint two independent, unrelated
    nodes with no shared identity at all."""
    emitter, _log_path = make_emitter()
    role_a = RoleRequirements(
        role_node_id=_ROLE_MANUFACTURER,
        role_name="Manufacturer",
        requirements=(("CRA_req_art_13.22", "Cooperate with market surveillance requests."),),
    )
    role_b = RoleRequirements(
        role_node_id=_ROLE_IMPORTER,
        role_name="Importer",
        requirements=(("CRA_req_art_19.7", "Cooperate with the market surveillance authority."),),
    )
    duty_text = "Cooperate with Market Surveillance Authority Requests"
    # ONE registry, ONE call to _derive_obligations, both Roles in the same
    # run -- this is what makes the collision detectable at all.
    call_completion = _scripted_sequential_call_completion(
        [_mint_response(duty_text), _mint_response(duty_text)]
    )

    obligation_nodes, has_edges, satisfied_by_edges, unmatched = _derive_obligations(
        (role_a, role_b), model="fake-model", call_completion=call_completion, emitter=emitter
    )

    assert unmatched == ()
    assert len(obligation_nodes) == 2

    node_a = next(n for n in obligation_nodes if n.properties["text"] == duty_text)
    qualified_text = f"{duty_text} as Importer"
    node_b = next(n for n in obligation_nodes if n.properties["text"] == qualified_text)

    # Distinct nodes, distinct ids.
    assert node_a.id != node_b.id
    assert node_a.id == obligation_id(duty_text)
    assert node_b.id == obligation_id(qualified_text)

    # Role A's HAS edge is untouched: exactly one HAS edge to node_a, still
    # pointing at Role A.
    edge_a = next(e for e in has_edges if e.obligation_node_id == node_a.id)
    assert edge_a.role_node_id == _ROLE_MANUFACTURER
    # Role B gets its OWN, separate HAS edge to the qualified node.
    edge_b = next(e for e in has_edges if e.obligation_node_id == node_b.id)
    assert edge_b.role_node_id == _ROLE_IMPORTER
    assert len(has_edges) == 2

    satisfied_a = next(e for e in satisfied_by_edges if e.requirement_id == "CRA_req_art_13.22")
    satisfied_b = next(e for e in satisfied_by_edges if e.requirement_id == "CRA_req_art_19.7")
    assert satisfied_a.obligation_node_id == node_a.id
    assert satisfied_b.obligation_node_id == node_b.id


def test_derive_obligations_malformed_response_marks_unmatched_without_aborting(
    make_emitter,
) -> None:
    """§7.5: a malformed/unparseable response is unified with the explicit
    unmatchable outcome -- surfaced, not silently dropped, and the run does
    not abort."""
    emitter, _log_path = make_emitter()
    role = RoleRequirements(
        role_node_id=_ROLE_MANUFACTURER,
        role_name="Manufacturer",
        requirements=(("CRA_req_art_13.1", "Some duty text."),),
    )
    call_completion = _scripted_sequential_call_completion(["{not valid json"])

    obligation_nodes, has_edges, satisfied_by_edges, unmatched = _derive_obligations(
        (role,), model="fake-model", call_completion=call_completion, emitter=emitter
    )

    assert obligation_nodes == ()
    assert has_edges == ()
    assert satisfied_by_edges == ()
    assert unmatched == ("CRA_req_art_13.1",)


def test_derive_obligations_emits_unmatched_log_entry(make_emitter, read_lines) -> None:
    emitter, log_path = make_emitter()
    role = RoleRequirements(
        role_node_id=_ROLE_MANUFACTURER,
        role_name="Manufacturer",
        requirements=(("CRA_req_art_13.9", "Vague text."),),
    )
    call_completion = _scripted_sequential_call_completion([_unmatchable_response()])

    _derive_obligations(
        (role,), model="fake-model", call_completion=call_completion, emitter=emitter
    )
    emitter.flush()

    lines = read_lines(log_path)
    unmatched_entries = [line for line in lines if line.get("outcome") == "unmatched"]
    assert len(unmatched_entries) == 1
    assert unmatched_entries[0]["entity_id"] == "CRA_req_art_13.9"
    assert unmatched_entries[0]["component"] == "domain_mapper"
    assert unmatched_entries[0]["action"] == "derive_obligations_and_capabilities"


# --- _derive_capabilities (Increment 14) ------------------------------------
#
# Per PLAN_REVIEWED.md §7.4/§11 Increment 14: dedup distinct Obligations
# first (by `.id`), one LLM call per distinct Obligation, single registry
# spanning the WHOLE run (all Roles' Obligations -- Capability convergence
# is deliberately Role- and Obligation-independent, unlike Obligation
# derivation's Role-scoped registry -- there is no role-qualification
# concept here at all).

_OBLIGATION_A = ObligationNode(
    id="obl_conduct_risk_assessment_aaaaaa",
    properties={"text": "Conduct a cybersecurity risk assessment.", "confidence": 0.9},
)
_OBLIGATION_B = ObligationNode(
    id="obl_report_security_incidents_bbbbbb",
    properties={"text": "Report security incidents to the authority.", "confidence": 0.9},
)


def _capability_match_response(matched_existing_id: str, confidence: float = 0.9) -> str:
    return json.dumps(
        {
            "capabilities": [
                {
                    "matched_existing_id": matched_existing_id,
                    "new_name": None,
                    "new_description": None,
                    "confidence": confidence,
                }
            ]
        }
    )


def _capability_mint_response(
    new_name: str, new_description: str | None = None, confidence: float = 0.9
) -> str:
    return json.dumps(
        {
            "capabilities": [
                {
                    "matched_existing_id": None,
                    "new_name": new_name,
                    "new_description": new_description,
                    "confidence": confidence,
                }
            ]
        }
    )


def _capability_multi_mint_response(
    names_and_descriptions: list[tuple[str, str | None]], confidence: float = 0.9
) -> str:
    return json.dumps(
        {
            "capabilities": [
                {
                    "matched_existing_id": None,
                    "new_name": name,
                    "new_description": description,
                    "confidence": confidence,
                }
                for name, description in names_and_descriptions
            ]
        }
    )


def test_derive_capabilities_two_distinct_obligations_converge_on_shared_capability(
    make_emitter,
) -> None:
    """Two distinct Obligations (conceptually from two different Roles --
    this function has no Role awareness at all) whose scripted responses
    BOTH mint the IDENTICAL Capability name -> the whole-run registry
    (keyed by identity.capability_id(name), §7.4) converges them onto ONE
    shared Capability node, with TWO REQUIRES edges, one per Obligation.
    Both calls MINT (not match) on purpose -- proving the CODE-level
    registry, not the model, guarantees convergence, mirroring
    _resolve_obligation_id's own philosophy for Obligation collisions."""
    emitter, _log_path = make_emitter()
    capability_name = "Access Control System"
    call_completion = _scripted_sequential_call_completion(
        [
            _capability_mint_response(capability_name),
            _capability_mint_response(capability_name),
        ]
    )

    capability_nodes, requires_edges = _derive_capabilities(
        (_OBLIGATION_A, _OBLIGATION_B),
        model="fake-model",
        call_completion=call_completion,
        emitter=emitter,
    )

    assert len(capability_nodes) == 1
    assert capability_nodes[0].id == capability_id(capability_name)
    assert capability_nodes[0].properties["name"] == capability_name
    assert len(requires_edges) == 2
    assert {e.obligation_node_id for e in requires_edges} == {_OBLIGATION_A.id, _OBLIGATION_B.id}
    assert all(e.capability_node_id == capability_nodes[0].id for e in requires_edges)


def test_derive_capabilities_one_obligation_two_capabilities_produces_two_requires_edges(
    make_emitter,
) -> None:
    """One Obligation's response lists TWO capabilities -> TWO REQUIRES
    edges off the SAME Obligation node, one new Capability minted for
    each -- multi-capability-per-Obligation support, a proven finding
    ported from spikes/cellar2/derive_capabilities.py and retained here
    per §7.4/§11 Increment 13's explicit instruction."""
    emitter, _log_path = make_emitter()
    call_completion = _scripted_sequential_call_completion(
        [
            _capability_multi_mint_response(
                [
                    ("Incident Detection", "Detects security incidents in real time."),
                    (
                        "Regulatory Notification Workflow",
                        "Notifies the relevant authority within the required window.",
                    ),
                ]
            )
        ]
    )

    capability_nodes, requires_edges = _derive_capabilities(
        (_OBLIGATION_A,), model="fake-model", call_completion=call_completion, emitter=emitter
    )

    assert len(capability_nodes) == 2
    assert {n.properties["name"] for n in capability_nodes} == {
        "Incident Detection",
        "Regulatory Notification Workflow",
    }
    assert len(requires_edges) == 2
    assert all(e.obligation_node_id == _OBLIGATION_A.id for e in requires_edges)
    assert {e.capability_node_id for e in requires_edges} == {n.id for n in capability_nodes}


def test_derive_capabilities_dedups_repeated_obligation_node_id_single_llm_call(
    make_emitter,
) -> None:
    """If the same obligation_node_id appears TWICE in the input Obligation
    list (e.g. because two Requirements both routed to it upstream), the
    LLM is called only ONCE for it -- proven by scripting exactly one
    response; an unscripted second call raises inside the structural fake
    (`_scripted_sequential_call_completion`'s own "no more scripted
    responses" guard). Since processing is keyed off the deduped list,
    exactly one Capability node and one REQUIRES edge result too, not
    two."""
    emitter, _log_path = make_emitter()
    call_completion = _scripted_sequential_call_completion(
        [_capability_mint_response("Access Control System")]
    )

    capability_nodes, requires_edges = _derive_capabilities(
        (_OBLIGATION_A, _OBLIGATION_A),
        model="fake-model",
        call_completion=call_completion,
        emitter=emitter,
    )

    assert len(capability_nodes) == 1
    assert len(requires_edges) == 1
    assert requires_edges[0].obligation_node_id == _OBLIGATION_A.id
    assert requires_edges[0].capability_node_id == capability_id("Access Control System")


def test_derive_capabilities_match_response_reuses_registry_entry(make_emitter) -> None:
    """A response that explicitly MATCHES an already-registered Capability
    (rather than re-minting identical text) resolves to the same node --
    the ordinary, model-cooperative convergence path, complementing the
    code-guaranteed convergence proven above."""
    emitter, _log_path = make_emitter()
    minted_name = "Access Control System"
    minted_id = capability_id(minted_name)
    call_completion = _scripted_sequential_call_completion(
        [
            _capability_mint_response(minted_name),
            _capability_match_response(minted_id),
        ]
    )

    capability_nodes, requires_edges = _derive_capabilities(
        (_OBLIGATION_A, _OBLIGATION_B),
        model="fake-model",
        call_completion=call_completion,
        emitter=emitter,
    )

    assert len(capability_nodes) == 1
    assert len(requires_edges) == 2
    assert {e.obligation_node_id for e in requires_edges} == {_OBLIGATION_A.id, _OBLIGATION_B.id}


# --- derive_obligations_and_capabilities (Increment 16) --------------------
#
# Per PLAN_REVIEWED.md §11 Increment 16: a hand-written structural fake for
# the baseline `GraphHandle`, scripted with the rows
# `_read_requirements_by_role`'s query expects, that also captures every
# other (write-side) query for assertion -- mirrors `test_extraction.py`'s
# `_FakeBaselineGraph` style, no `unittest.mock`.


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
    """Satisfies `GraphHandle` structurally. Answers
    `_read_requirements_by_role`'s `OPTIONAL MATCH (rl:Role...` query with a
    scripted row set; captures every other `(query, params)` call -- the
    write-side calls `persist_obligation_and_capability_graph` issues --
    for assertion."""

    def __init__(self, requirement_rows: list[list[object]]) -> None:
        self._requirement_rows = requirement_rows
        self.calls: list[_RecordedCall] = []

    def query(self, q: str, params: dict[str, object] | None = None) -> _FakeQueryResult:
        if "OPTIONAL MATCH (rl:Role" in q:
            return _FakeQueryResult(self._requirement_rows)
        self.calls.append(_RecordedCall(q, params))
        return _FakeQueryResult([[0]])


def _find_edge_calls(graph: _FakeBaselineGraph, relationship_type: str) -> list[_RecordedCall]:
    return [call for call in graph.calls if f"[:{relationship_type}]" in call.query]


def _never_called_completion(invocations: list[bool]) -> CompletionCaller:
    """A `CompletionCaller` fake that raises if it is EVER invoked --
    Increment 16 test (e)'s direct proof that the dangling-`role_id` check
    runs before any LLM call is made for any Role's Requirements."""

    def _call(*, model: str, messages: list[dict[str, str]], timeout: float) -> ModelResponse:
        invocations.append(True)
        raise AssertionError("call_completion should never be invoked")

    return _call


def test_derive_obligations_and_capabilities_ac003_full_flow(make_emitter) -> None:
    """(a) AC-003: 2 Roles, 3 Requirements, all matchable -> every
    Requirement has >=1 SATISFIED_BY, every Obligation has exactly 1 HAS
    from its Role, every Obligation has >=1 REQUIRES."""
    emitter, _log_path = make_emitter()
    rows: list[list[object]] = [
        [
            "CRA_req_art_13.1",
            "Conduct a cybersecurity risk assessment.",
            _ROLE_MANUFACTURER,
            _ROLE_MANUFACTURER,
            "Manufacturer",
        ],
        [
            "CRA_req_art_13.2",
            "Keep the risk assessment documented and updated.",
            _ROLE_MANUFACTURER,
            _ROLE_MANUFACTURER,
            "Manufacturer",
        ],
        [
            "CRA_req_art_14.1",
            "Verify the manufacturer's conformity assessment.",
            _ROLE_IMPORTER,
            _ROLE_IMPORTER,
            "Importer",
        ],
    ]
    baseline_graph = _FakeBaselineGraph(rows)
    call_completion = _scripted_sequential_call_completion(
        [
            _mint_response("Conduct Cybersecurity Risk Assessment"),
            _mint_response("Maintain Risk Assessment Records"),
            _mint_response("Verify Conformity Assessment"),
            _capability_mint_response("Risk Assessment Tooling"),
            _capability_mint_response("Documentation System"),
            _capability_mint_response("Conformity Verification System"),
        ]
    )

    result = derive_obligations_and_capabilities(
        "CRA-1.0",
        baseline_graph=baseline_graph,
        model="fake-model",
        call_completion=call_completion,
        emitter=emitter,
    )

    assert result.unmatched_requirement_ids == ()
    assert len(result.obligation_node_ids) == 3
    assert len(result.capability_node_ids) == 3

    satisfied_by_calls = _find_edge_calls(baseline_graph, "SATISFIED_BY")
    has_calls = _find_edge_calls(baseline_graph, "HAS")
    requires_calls = _find_edge_calls(baseline_graph, "REQUIRES")

    satisfied_source_ids = {
        call.params["source_id"] for call in satisfied_by_calls if call.params
    }
    assert satisfied_source_ids == {
        "CRA_req_art_13.1",
        "CRA_req_art_13.2",
        "CRA_req_art_14.1",
    }

    for obligation_node_id in result.obligation_node_ids:
        has_targets = [
            call
            for call in has_calls
            if call.params and call.params["target_id"] == obligation_node_id
        ]
        assert len(has_targets) == 1
        requires_sources = [
            call
            for call in requires_calls
            if call.params and call.params["source_id"] == obligation_node_id
        ]
        assert len(requires_sources) >= 1


def test_derive_obligations_and_capabilities_ac004_unmatched_requirement_surfaced(
    make_emitter, read_lines
) -> None:
    """(b) AC-004: one Requirement's scripted response is unmatchable -> it
    appears in DerivationResult.unmatched_requirement_ids, has no
    SATISFIED_BY edge written, and an outcome="unmatched" log entry is
    emitted naming its id."""
    emitter, log_path = make_emitter()
    rows: list[list[object]] = [
        [
            "CRA_req_art_13.1",
            "Conduct a cybersecurity risk assessment.",
            _ROLE_MANUFACTURER,
            _ROLE_MANUFACTURER,
            "Manufacturer",
        ],
        [
            "CRA_req_art_13.9",
            "Some vague, unclear text.",
            _ROLE_MANUFACTURER,
            _ROLE_MANUFACTURER,
            "Manufacturer",
        ],
    ]
    baseline_graph = _FakeBaselineGraph(rows)
    call_completion = _scripted_sequential_call_completion(
        [
            _mint_response("Conduct Cybersecurity Risk Assessment"),
            _unmatchable_response(),
            _capability_mint_response("Risk Assessment Tooling"),
        ]
    )

    result = derive_obligations_and_capabilities(
        "CRA-1.0",
        baseline_graph=baseline_graph,
        model="fake-model",
        call_completion=call_completion,
        emitter=emitter,
    )
    emitter.flush()

    assert result.unmatched_requirement_ids == ("CRA_req_art_13.9",)

    satisfied_by_calls = _find_edge_calls(baseline_graph, "SATISFIED_BY")
    satisfied_source_ids = {
        call.params["source_id"] for call in satisfied_by_calls if call.params
    }
    assert "CRA_req_art_13.9" not in satisfied_source_ids

    lines = read_lines(log_path)
    unmatched_entries = [line for line in lines if line.get("outcome") == "unmatched"]
    assert len(unmatched_entries) == 1
    assert unmatched_entries[0]["entity_id"] == "CRA_req_art_13.9"


def test_derive_obligations_and_capabilities_ac007_emits_log_entry_with_bound_run_id(
    make_emitter, read_lines
) -> None:
    """(c) AC-007: mirrors the AC-006 pattern exactly."""
    emitter, log_path = make_emitter()
    rows: list[list[object]] = [
        [
            "CRA_req_art_13.1",
            "Conduct a cybersecurity risk assessment.",
            _ROLE_MANUFACTURER,
            _ROLE_MANUFACTURER,
            "Manufacturer",
        ],
    ]
    baseline_graph = _FakeBaselineGraph(rows)
    call_completion = _scripted_sequential_call_completion(
        [
            _mint_response("Conduct Cybersecurity Risk Assessment"),
            _capability_mint_response("Risk Assessment Tooling"),
        ]
    )

    with bind_run_context("run-x"):
        derive_obligations_and_capabilities(
            "CRA-1.0",
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
    assert succeeded_entries[0]["action"] == "derive_obligations_and_capabilities"
    assert succeeded_entries[0]["entity_id"] == "CRA-1.0"


def test_derive_obligations_and_capabilities_all_requirements_unmatchable_for_role(
    make_emitter,
) -> None:
    """(d) A Role with 2 Requirements, both scripted unmatchable -> both ids
    in unmatched_requirement_ids, zero Obligation nodes/HAS edges for that
    Role (permitted -- HAS is 1:0..*), no exception."""
    emitter, _log_path = make_emitter()
    rows: list[list[object]] = [
        [
            "CRA_req_art_13.1",
            "Vague text one.",
            _ROLE_MANUFACTURER,
            _ROLE_MANUFACTURER,
            "Manufacturer",
        ],
        [
            "CRA_req_art_13.2",
            "Vague text two.",
            _ROLE_MANUFACTURER,
            _ROLE_MANUFACTURER,
            "Manufacturer",
        ],
    ]
    baseline_graph = _FakeBaselineGraph(rows)
    call_completion = _scripted_sequential_call_completion(
        [_unmatchable_response(), _unmatchable_response()]
    )

    result = derive_obligations_and_capabilities(
        "CRA-1.0",
        baseline_graph=baseline_graph,
        model="fake-model",
        call_completion=call_completion,
        emitter=emitter,
    )

    assert set(result.unmatched_requirement_ids) == {"CRA_req_art_13.1", "CRA_req_art_13.2"}
    assert result.obligation_node_ids == ()
    assert _find_edge_calls(baseline_graph, "HAS") == []


def test_derive_obligations_and_capabilities_dangling_role_id_raises_before_any_llm_call(
    make_emitter,
) -> None:
    """(e) A fake baseline graph scripted so one Requirement's role_id does
    not resolve to any Role node -> raises DomainMapperDerivationError
    naming the Requirement id and the dangling role_id, BEFORE any LLM call
    is made (the structural fake for call_completion is never invoked)."""
    emitter, _log_path = make_emitter()
    rows: list[list[object]] = [
        ["CRA_req_art_13.1", "Some duty.", "role_ghost_999", None, None],
    ]
    baseline_graph = _FakeBaselineGraph(rows)
    invocations: list[bool] = []
    call_completion = _never_called_completion(invocations)

    with pytest.raises(DomainMapperDerivationError) as exc_info:
        derive_obligations_and_capabilities(
            "CRA-1.0",
            baseline_graph=baseline_graph,
            model="fake-model",
            call_completion=call_completion,
            emitter=emitter,
        )

    assert invocations == []
    assert "CRA_req_art_13.1" in str(exc_info.value)
    assert "role_ghost_999" in str(exc_info.value)
    assert baseline_graph.calls == []
