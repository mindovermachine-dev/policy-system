"""Tests for ps_service.domain_mapper.derivation._derive_obligations.

Per PLAN_REVIEWED.md §11 Increment 12 / the binding testing convention
(§0.3/§0.5): `call_completion` is faked with a hand-written structural fake
satisfying `CompletionCaller`'s Protocol, scripted per-call in Role-then-
Requirement document order — never `unittest.mock.Mock`/`MagicMock`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

import httpx
import openai
import pytest
from litellm.types.utils import Choices, Message, ModelResponse

from ps_service.domain_mapper.derivation import (
    _derive_capabilities,  # pyright: ignore[reportPrivateUsage]  # test drives this module-internal helper directly (see module docstring)
    _derive_obligations,  # pyright: ignore[reportPrivateUsage]  # test drives this module-internal helper directly (see module docstring)
    _to_capability_node,  # pyright: ignore[reportPrivateUsage]  # test drives this module-internal helper directly, mirrors test_extraction.py's identical pattern for _build_requirement_graph
    derive_obligations_and_capabilities,
)
from ps_service.domain_mapper.errors import DomainMapperDerivationError
from ps_service.domain_mapper.identity import capability_id, obligation_id
from ps_service.domain_mapper.models import CapabilityDecision, ObligationNode, RoleRequirements
from ps_service.domain_mapper.prompts import (
    CAPABILITY_DERIVATION_SYSTEM_PROMPT,
    CAPABILITY_REUSE_VERIFICATION_SYSTEM_PROMPT,
    OBLIGATION_DERIVATION_SYSTEM_PROMPT,
)
from ps_service.llm_interface.errors import LlmProviderError
from ps_service.logging import bind_run_context

if TYPE_CHECKING:
    from collections.abc import Mapping

    from domain_mapper._fakes import MakeEmitter, ReadLines
    from ps_service.domain_mapper.models import DerivationResult
    from ps_service.llm_interface.client import CompletionCaller
    from ps_service.logging import LogEmitter


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
    """A `CompletionCaller` fake that returns `responses` in the exact
    order `_derive_obligations`/`_derive_capabilities` calls the LLM — Role
    then Requirement document order (PLAN_REVIEWED.md §7.3), or Obligation
    processing order for Capability derivation. Raises if more calls are
    made than were scripted, so an unexpected extra call fails loudly
    rather than silently reusing a stale response.

    An item may be an `Exception` instance instead of a response string —
    the fake raises it directly rather than returning a canned
    `ModelResponse`, to simulate a genuine infra failure (e.g.
    `openai.APIConnectionError`, issue #64 slice 7) partway through a
    sequence.
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


def _recording_sequential_call_completion(
    responses: list[str], recorded_messages: list[list[dict[str, str]]]
) -> CompletionCaller:
    """A `CompletionCaller` fake that returns `responses` in order (like
    `_scripted_sequential_call_completion`) and appends each call's
    `messages` to `recorded_messages`, so a test can assert on what was
    actually sent to the LLM boundary. Raises on an unscripted extra call.
    """
    remaining = list(responses)

    def _call(*, model: str, messages: list[dict[str, str]], timeout: float) -> ModelResponse:
        if not remaining:
            raise AssertionError("no more scripted responses -- unexpected extra LLM call")
        recorded_messages.append([dict(message) for message in messages])
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


def test_derive_obligations_second_requirement_matches_first_minted_entry(
    make_emitter: MakeEmitter,
) -> None:
    """(a) Single Role, first Requirement mints (registry empty), second
    (matching-duty) Requirement matches the registry entry call 1 minted.
    """
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
    minted_id = obligation_id(_ROLE_MANUFACTURER, minted_text)
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
    make_emitter: MakeEmitter,
) -> None:
    """(b) An unmatchable response produces obligation_node_id=None with no
    exception -- no Obligation is created, the Requirement is surfaced in
    unmatched_requirement_ids instead.
    """
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
    make_emitter: MakeEmitter,
) -> None:
    """(c) Two Requirements under the SAME Role independently produce
    identical mint text (two separate LLM calls, both minting, not
    matching) -> a single Obligation node results because the second
    call's obligation_id() collides with the registry entry the FIRST call
    created for the SAME role, so it's reused, not re-minted. One HAS
    edge, both Requirements SATISFIED_BY it, no role-qualification.
    """
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
    assert obligation_nodes[0].id == obligation_id(_ROLE_MANUFACTURER, duty_text)
    assert len(has_edges) == 1
    assert has_edges[0].role_node_id == _ROLE_MANUFACTURER
    assert len(satisfied_by_edges) == 2
    assert {e.obligation_node_id for e in satisfied_by_edges} == {obligation_nodes[0].id}
    assert unmatched == ()


def test_derive_obligations_same_duty_text_under_two_roles_is_two_distinct_nodes(
    make_emitter: MakeEmitter,
) -> None:
    """(d) #42's resolution: Role A's Requirement mints "Cooperate with
    Market Surveillance Authority Requests" (one HAS edge to Role A). Role B,
    processed later in the same run, independently mints the IDENTICAL text.
    Because `obligation_id` is Role-scoped, the two ids differ by
    construction — two distinct Obligation nodes, each with its own single
    HAS edge, each keeping the LLM's own unqualified duty text. No
    `" as {role}"` text mangling, no runtime collision detection.
    """
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
    call_completion = _scripted_sequential_call_completion(
        [_mint_response(duty_text), _mint_response(duty_text)]
    )

    obligation_nodes, has_edges, satisfied_by_edges, unmatched = _derive_obligations(
        (role_a, role_b), model="fake-model", call_completion=call_completion, emitter=emitter
    )

    assert unmatched == ()
    assert len(obligation_nodes) == 2

    # Both nodes carry the LLM's own unqualified text — the Role only enters
    # the id hash, never the text.
    assert [n.properties["text"] for n in obligation_nodes] == [duty_text, duty_text]

    node_a = next(
        n for n in obligation_nodes if n.id == obligation_id(_ROLE_MANUFACTURER, duty_text)
    )
    node_b = next(n for n in obligation_nodes if n.id == obligation_id(_ROLE_IMPORTER, duty_text))
    assert node_a.id != node_b.id

    # Each Role gets exactly one HAS edge, to its own node.
    edge_a = next(e for e in has_edges if e.obligation_node_id == node_a.id)
    assert edge_a.role_node_id == _ROLE_MANUFACTURER
    edge_b = next(e for e in has_edges if e.obligation_node_id == node_b.id)
    assert edge_b.role_node_id == _ROLE_IMPORTER
    assert len(has_edges) == 2

    satisfied_a = next(e for e in satisfied_by_edges if e.requirement_id == "CRA_req_art_13.22")
    satisfied_b = next(e for e in satisfied_by_edges if e.requirement_id == "CRA_req_art_19.7")
    assert satisfied_a.obligation_node_id == node_a.id
    assert satisfied_b.obligation_node_id == node_b.id


def test_derive_obligations_malformed_response_marks_unmatched_without_aborting(
    make_emitter: MakeEmitter,
) -> None:
    """§7.5: a malformed/unparseable response is unified with the explicit
    unmatchable outcome -- surfaced, not silently dropped, and the run does
    not abort.
    """
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


def test_derive_obligations_emits_unmatched_log_entry(
    make_emitter: MakeEmitter, read_lines: ReadLines
) -> None:
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


# --- _to_capability_node (issue #109) ---------------------------------------


def test_to_capability_node_stamps_status_active_at_mint_time() -> None:
    """AC-BI-002: every newly-minted CapabilityNode carries status="active" —
    same ingest-time-active rule as RequirementNode (extraction.py), mirroring
    ingestion's RegulatoryInstrument.status pattern (graph_writer.py:163).
    """
    decision = CapabilityDecision(
        obligation_node_id="obl_risk_management_abc123",
        capability_node_id=capability_id("Security Logging"),
        name="Security Logging",
        description=None,
        confidence=0.85,
    )

    node = _to_capability_node(decision)

    assert node.properties["status"] == "active"


def test_capability_active_only_filter_returns_newly_minted_but_not_legacy_null_status() -> None:
    """AC-BI-003 (Capability half): filter-semantics proxy test
    (documentation only — no production filter exists; see PLAN.md §0 /
    CHANGES.md Resolution 2). This test does not call or exercise any
    production filter code — none exists in ps-qna or
    `ps_service/query_engine` (confirmed in §0/Critique); `active_only_filter()`
    is a lambda defined locally inside the test purely to document and verify
    the predicate semantics AC-BI-003 depends on, not to test a real filter
    implementation. Same predicate shape as
    test_requirement_active_only_filter_returns_newly_minted_but_not_legacy_null_status
    (test_extraction.py) — mirrors ps-qna/SKILL.md:86-107's
    `WHERE n.status = 'active'` against in-memory properties dicts, no live
    graph required.
    """
    decision = CapabilityDecision(
        obligation_node_id="obl_risk_management_abc123",
        capability_node_id=capability_id("Security Logging"),
        name="Security Logging",
        description=None,
        confidence=0.85,
    )
    newly_minted = _to_capability_node(decision).properties
    legacy_pre_fix = {"name": "Old Capability", "confidence": 0.7}

    def active_only_filter(properties: Mapping[str, object]) -> bool:
        return properties.get("status") == "active"

    assert active_only_filter(newly_minted) is True
    assert active_only_filter(legacy_pre_fix) is False


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
_OBLIGATION_C = ObligationNode(
    id="obl_maintain_incident_log_cccccc",
    properties={"text": "Maintain a log of all security incidents.", "confidence": 0.9},
)
_HALLUCINATED_CAPABILITY_ID = "cap_accountability_compliance"  # TASK.md:7 — the exact id the
# live flake observed; matched_existing_id set, absent from the registry, new_name=None.


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


def _reuse_accept_response(confidence: float | None = 0.9) -> str:
    return json.dumps(
        {"verdict": "accept", "new_name": None, "new_description": None, "confidence": confidence}
    )


type _RecordedByPrompt = dict[str, list[list[dict[str, str]]]]


def _prompt_routed_call_completion(
    *,
    obligation: list[str],
    capability: list[str],
    verification: list[str | Exception],
) -> tuple[CompletionCaller, _RecordedByPrompt]:
    """A `CompletionCaller` fake that routes each call on its system prompt
    (`messages[0]["content"]`) to the matching scripted queue -- obligation
    derivation, capability derivation or reuse verification -- and records
    each call's `messages` per queue, in call order. Raises `AssertionError`
    on an unknown system prompt or an unscripted call (the same technique as
    `test_ingest_regulation_tool.py`'s prompt-routed fake).
    """
    routes = {
        OBLIGATION_DERIVATION_SYSTEM_PROMPT: "obligation",
        CAPABILITY_DERIVATION_SYSTEM_PROMPT: "capability",
        CAPABILITY_REUSE_VERIFICATION_SYSTEM_PROMPT: "verification",
    }
    queues: dict[str, list[str | Exception]] = {
        "obligation": [*obligation],
        "capability": [*capability],
        "verification": [*verification],
    }
    recorded: _RecordedByPrompt = {"obligation": [], "capability": [], "verification": []}

    def _call(*, model: str, messages: list[dict[str, str]], timeout: float) -> ModelResponse:
        route = routes.get(messages[0]["content"])
        if route is None:
            raise AssertionError("unknown system prompt -- unexpected LLM call")
        queue = queues[route]
        if not queue:
            raise AssertionError(f"no more scripted {route} responses -- unexpected extra call")
        recorded[route].append([dict(message) for message in messages])
        next_item = queue.pop(0)
        if isinstance(next_item, Exception):
            raise next_item
        return _model_response(next_item)

    return _call, recorded


def test_derive_capabilities_two_distinct_obligations_converge_on_shared_capability(
    make_emitter: MakeEmitter,
) -> None:
    """Two distinct Obligations (conceptually from two different Roles --
    this function has no Role awareness at all) whose scripted responses
    BOTH mint the IDENTICAL Capability name -> the whole-run registry
    (keyed by identity.capability_id(name), §7.4) converges them onto ONE
    shared Capability node, with TWO REQUIRES edges, one per Obligation.
    Both calls MINT (not match) on purpose -- proving the CODE-level
    registry, not the model, guarantees convergence, mirroring
    _resolve_obligation_id's own same-Role reuse philosophy. B's identical
    mint is verified as a reuse (#187) and accepted.
    """
    emitter, _log_path = make_emitter()
    capability_name = "Access Control System"
    call_completion = _scripted_sequential_call_completion(
        [
            _capability_mint_response(capability_name),
            _capability_mint_response(capability_name),
            _reuse_accept_response(),
        ]
    )

    capability_nodes, requires_edges, unmatched_obligation_ids = _derive_capabilities(
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
    assert unmatched_obligation_ids == ()


def test_derive_capabilities_one_obligation_two_capabilities_produces_two_requires_edges(
    make_emitter: MakeEmitter,
) -> None:
    """One Obligation's response lists TWO capabilities -> TWO REQUIRES
    edges off the SAME Obligation node, one new Capability minted for
    each -- multi-capability-per-Obligation support, a proven finding
    ported from spikes/cellar2/derive_capabilities.py and retained here
    per §7.4/§11 Increment 13's explicit instruction.
    """
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

    capability_nodes, requires_edges, unmatched_obligation_ids = _derive_capabilities(
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
    assert unmatched_obligation_ids == ()


def test_derive_capabilities_dedups_repeated_obligation_node_id_single_llm_call(
    make_emitter: MakeEmitter,
) -> None:
    """If the same obligation_node_id appears TWICE in the input Obligation
    list (e.g. because two Requirements both routed to it upstream), the
    LLM is called only ONCE for it -- proven by scripting exactly one
    response; an unscripted second call raises inside the structural fake
    (`_scripted_sequential_call_completion`'s own "no more scripted
    responses" guard). Since processing is keyed off the deduped list,
    exactly one Capability node and one REQUIRES edge result too, not
    two.
    """
    emitter, _log_path = make_emitter()
    call_completion = _scripted_sequential_call_completion(
        [_capability_mint_response("Access Control System")]
    )

    capability_nodes, requires_edges, unmatched_obligation_ids = _derive_capabilities(
        (_OBLIGATION_A, _OBLIGATION_A),
        model="fake-model",
        call_completion=call_completion,
        emitter=emitter,
    )

    assert len(capability_nodes) == 1
    assert len(requires_edges) == 1
    assert requires_edges[0].obligation_node_id == _OBLIGATION_A.id
    assert requires_edges[0].capability_node_id == capability_id("Access Control System")
    assert unmatched_obligation_ids == ()


def test_derive_capabilities_match_response_reuses_registry_entry(
    make_emitter: MakeEmitter,
) -> None:
    """A response that explicitly MATCHES an already-registered Capability
    (rather than re-minting identical text) resolves to the same node --
    the ordinary, model-cooperative convergence path, complementing the
    code-guaranteed convergence proven above. B's match is verified as a
    reuse (#187) and accepted.
    """
    emitter, _log_path = make_emitter()
    minted_name = "Access Control System"
    minted_id = capability_id(minted_name)
    call_completion = _scripted_sequential_call_completion(
        [
            _capability_mint_response(minted_name),
            _capability_match_response(minted_id),
            _reuse_accept_response(),
        ]
    )

    capability_nodes, requires_edges, unmatched_obligation_ids = _derive_capabilities(
        (_OBLIGATION_A, _OBLIGATION_B),
        model="fake-model",
        call_completion=call_completion,
        emitter=emitter,
    )

    assert len(capability_nodes) == 1
    assert len(requires_edges) == 2
    assert {e.obligation_node_id for e in requires_edges} == {_OBLIGATION_A.id, _OBLIGATION_B.id}
    assert unmatched_obligation_ids == ()


def test_derive_capabilities_malformed_response_marks_unmatched_without_aborting(
    make_emitter: MakeEmitter,
) -> None:
    """Issue #64 / AC-BI-001, AC-BI-002: a malformed/unparseable Capability
    response for one Obligation is isolated -- surfaced via
    `unmatched_obligation_ids`, not a silently dropped Obligation and not an
    uncaught exception that aborts the whole run.
    """
    emitter, _log_path = make_emitter()
    call_completion = _scripted_sequential_call_completion(["{not valid json"])

    capability_nodes, requires_edges, unmatched_obligation_ids = _derive_capabilities(
        (_OBLIGATION_A,), model="fake-model", call_completion=call_completion, emitter=emitter
    )

    assert capability_nodes == ()
    assert requires_edges == ()
    assert unmatched_obligation_ids == (_OBLIGATION_A.id,)


def test_derive_capabilities_emits_unmatched_log_entry(
    make_emitter: MakeEmitter, read_lines: ReadLines
) -> None:
    """Issue #64 / AC-BI-002: the isolated failure emits exactly one
    `outcome="unmatched"` log entry keyed by the failing Obligation's id.
    """
    emitter, log_path = make_emitter()
    call_completion = _scripted_sequential_call_completion(["{not valid json"])

    _derive_capabilities(
        (_OBLIGATION_A,), model="fake-model", call_completion=call_completion, emitter=emitter
    )
    emitter.flush()

    lines = read_lines(log_path)
    unmatched_entries = [line for line in lines if line.get("outcome") == "unmatched"]
    assert len(unmatched_entries) == 1
    assert unmatched_entries[0]["entity_id"] == _OBLIGATION_A.id
    assert unmatched_entries[0]["component"] == "domain_mapper"
    assert unmatched_entries[0]["action"] == "derive_obligations_and_capabilities"


def test_derive_capabilities_two_of_three_obligations_converge_despite_one_malformed_response(
    make_emitter: MakeEmitter,
) -> None:
    """Issue #64 / AC-BI-003: three distinct Obligations processed in order
    A, C, B. A and B's responses both MINT the identical Capability name
    (same code-level-convergence technique as
    `test_derive_capabilities_two_distinct_obligations_converge_on_shared_capability`),
    while C -- in between -- gets a malformed response. The failure for C
    neither poisons nor skips the whole-run registry state built up by A
    and consumed by B: A and B still converge onto ONE shared Capability
    node with TWO REQUIRES edges, and C alone is surfaced as unmatched.
    B's identical mint is verified as a reuse (#187) and accepted.
    """
    emitter, _log_path = make_emitter()
    capability_name = "Access Control System"
    call_completion = _scripted_sequential_call_completion(
        [
            _capability_mint_response(capability_name),
            "{not valid json",
            _capability_mint_response(capability_name),
            _reuse_accept_response(),
        ]
    )

    capability_nodes, requires_edges, unmatched_obligation_ids = _derive_capabilities(
        (_OBLIGATION_A, _OBLIGATION_C, _OBLIGATION_B),
        model="fake-model",
        call_completion=call_completion,
        emitter=emitter,
    )

    assert len(capability_nodes) == 1
    assert capability_nodes[0].id == capability_id(capability_name)
    assert len(requires_edges) == 2
    assert {e.obligation_node_id for e in requires_edges} == {_OBLIGATION_A.id, _OBLIGATION_B.id}
    assert all(e.capability_node_id == capability_nodes[0].id for e in requires_edges)
    assert unmatched_obligation_ids == (_OBLIGATION_C.id,)


def test_derive_capabilities_dedups_repeated_obligation_node_id_even_when_it_fails(
    make_emitter: MakeEmitter,
) -> None:
    """Issue #64 / AC-BI-003 completeness: the same obligation_node_id
    appearing TWICE in the input still results in exactly ONE LLM call even
    when that one call's response is malformed -- proven by scripting
    exactly one response; an unscripted second call raises inside the
    structural fake's own "no more scripted responses" guard (same
    technique as
    `test_derive_capabilities_dedups_repeated_obligation_node_id_single_llm_call`).
    The failing Obligation id appears exactly once in
    unmatched_obligation_ids, not once per repetition.
    """
    emitter, _log_path = make_emitter()
    call_completion = _scripted_sequential_call_completion(["{not valid json"])

    capability_nodes, requires_edges, unmatched_obligation_ids = _derive_capabilities(
        (_OBLIGATION_A, _OBLIGATION_A),
        model="fake-model",
        call_completion=call_completion,
        emitter=emitter,
    )

    assert capability_nodes == ()
    assert requires_edges == ()
    assert unmatched_obligation_ids == (_OBLIGATION_A.id,)


def test_derive_capabilities_hallucinated_capability_match_isolated_within_batch(
    make_emitter: MakeEmitter,
) -> None:
    """#45 AC-BI-001 + AC-BI-002: a well-formed capability-derivation response whose
    matched_existing_id names a Capability absent from the registry, with no usable
    new_name, is the exact hallucination shape TASK.md:7 reports from the live capstone
    flake -- distinct from issue #64's bad-JSON trigger. Three distinct Obligations
    processed in order A, C, B: A and B's responses both MINT the identical Capability
    name (same code-level-convergence technique as
    test_derive_capabilities_two_distinct_obligations_converge_on_shared_capability),
    while C -- in between -- gets the hallucinated matched_existing_id. The failure for
    C neither poisons nor skips the whole-run registry state A built and B consumes: A
    and B still converge onto ONE shared Capability node with TWO REQUIRES edges, and C
    alone is surfaced as unmatched with no Capability node or REQUIRES edge of its own --
    proving _process_obligation's except DomainMapperDerivationError catch
    (derivation.py:598-609) reaches this specific well-formed-but-unresolvable trigger,
    not just malformed JSON. B's identical mint is verified as a reuse (#187) and
    accepted.
    """
    emitter, _log_path = make_emitter()
    capability_name = "Access Control System"
    call_completion = _scripted_sequential_call_completion(
        [
            _capability_mint_response(capability_name),
            _capability_match_response(_HALLUCINATED_CAPABILITY_ID),
            _capability_mint_response(capability_name),
            _reuse_accept_response(),
        ]
    )

    capability_nodes, requires_edges, unmatched_obligation_ids = _derive_capabilities(
        (_OBLIGATION_A, _OBLIGATION_C, _OBLIGATION_B),
        model="fake-model",
        call_completion=call_completion,
        emitter=emitter,
    )

    # AC-BI-001: the hallucinated-id Obligation surfaces unmatched, nothing else for it.
    assert unmatched_obligation_ids == (_OBLIGATION_C.id,)
    assert all(edge.obligation_node_id != _OBLIGATION_C.id for edge in requires_edges)
    assert _HALLUCINATED_CAPABILITY_ID not in {node.id for node in capability_nodes}

    # AC-BI-002: the rest of the batch is unaffected -- normal return, no exception, A/B
    # still converge correctly.
    assert len(capability_nodes) == 1
    assert capability_nodes[0].id == capability_id(capability_name)
    assert len(requires_edges) == 2
    assert {edge.obligation_node_id for edge in requires_edges} == {
        _OBLIGATION_A.id,
        _OBLIGATION_B.id,
    }
    assert all(edge.capability_node_id == capability_nodes[0].id for edge in requires_edges)


def test_derive_capabilities_hallucinated_capability_match_emits_unmatched_log_entry(
    make_emitter: MakeEmitter, read_lines: ReadLines
) -> None:
    """#45 AC-BI-003: the hallucinated-id Obligation from the same A/C/B batch as
    test_derive_capabilities_hallucinated_capability_match_isolated_within_batch emits
    EXACTLY ONE outcome="unmatched" log entry, keyed by its own entity_id -- and A/B's
    successful mint/match decisions do not also produce unmatched entries alongside it.
    Mirrors test_derive_capabilities_emits_unmatched_log_entry's assertion shape
    (test_derivation.py:555-574), the issue #64 precedent for this exact log-assertion
    style, extended to a 3-Obligation batch so "exactly one" is actually exercised
    against a run where other entries could plausibly appear. B's identical mint is
    verified as a reuse (#187) and accepted; that verdict line has
    outcome="reuse_accepted", so "exactly one unmatched" still holds.
    """
    emitter, log_path = make_emitter()
    capability_name = "Access Control System"
    call_completion = _scripted_sequential_call_completion(
        [
            _capability_mint_response(capability_name),
            _capability_match_response(_HALLUCINATED_CAPABILITY_ID),
            _capability_mint_response(capability_name),
            _reuse_accept_response(),
        ]
    )

    _derive_capabilities(
        (_OBLIGATION_A, _OBLIGATION_C, _OBLIGATION_B),
        model="fake-model",
        call_completion=call_completion,
        emitter=emitter,
    )
    emitter.flush()

    lines = read_lines(log_path)
    unmatched_entries = [line for line in lines if line.get("outcome") == "unmatched"]
    assert len(unmatched_entries) == 1
    assert unmatched_entries[0]["entity_id"] == _OBLIGATION_C.id
    assert unmatched_entries[0]["component"] == "domain_mapper"
    assert unmatched_entries[0]["action"] == "derive_obligations_and_capabilities"


def test_derive_capabilities_propagates_llm_provider_error_and_aborts(
    make_emitter: MakeEmitter,
) -> None:
    """Issue #64 / AC-BI-004: a genuine LLM Interface infra failure
    (`openai.APIConnectionError`, wrapped by `route_completion` into
    `LlmProviderError`) for one Obligation's call is NOT caught by
    `_process_obligation`'s `try/except DomainMapperDerivationError` --
    it propagates unchanged and aborts the whole `_derive_capabilities`
    call, proving the isolation added for issue #64 does not accidentally
    widen to catch infra failures too.
    """
    emitter, _log_path = make_emitter()
    call_completion = _scripted_sequential_call_completion(
        [openai.APIConnectionError(request=httpx.Request("POST", "https://example.invalid"))]
    )

    with pytest.raises(LlmProviderError):
        _derive_capabilities(
            (_OBLIGATION_A, _OBLIGATION_B),
            model="fake-model",
            call_completion=call_completion,
            emitter=emitter,
        )


def test_derive_capabilities_propagates_llm_provider_error_for_empty_completion_content_and_aborts(
    make_emitter: MakeEmitter,
) -> None:
    """Issue #64 / AC-BI-004 (CHANGES.md item 1): an empty-string completion
    content is a distinct `LlmProviderError` trigger from
    `openai.APIConnectionError` above -- `route_completion`'s
    `_to_completion_result` (`completion.py:62-63`) raises `LlmProviderError`
    itself, outside `route_completion`'s own `except openai.OpenAIError`
    block, when the provider returns a response with empty completion text.
    This too must propagate uncaught through `_process_obligation` and
    abort the whole call.
    """
    emitter, _log_path = make_emitter()
    call_completion = _scripted_sequential_call_completion([""])

    with pytest.raises(LlmProviderError):
        _derive_capabilities(
            (_OBLIGATION_A,), model="fake-model", call_completion=call_completion, emitter=emitter
        )


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
    for assertion.
    """

    def __init__(self, requirement_rows: list[list[object]]) -> None:
        self._requirement_rows: list[object] = [*requirement_rows]
        self.calls: list[_RecordedCall] = []

    def query(self, q: str, params: dict[str, object] | None = None) -> _FakeQueryResult:
        if "OPTIONAL MATCH (rl:Role" in q:
            return _FakeQueryResult(self._requirement_rows)
        self.calls.append(_RecordedCall(q, params))
        return _FakeQueryResult([[0]])


class _VersionedBaselineGraph(_FakeBaselineGraph):
    """A baseline graph holding two instrument versions' Requirements side by side.

    A requirement read naming `{id: $rid}` answers with that version's rows only; an unscoped
    read answers with every version's rows -- the leak the scoping closes.
    """

    def __init__(self, rows_by_instrument: dict[str, list[list[object]]]) -> None:
        super().__init__([row for rows in rows_by_instrument.values() for row in rows])
        self._rows_by_instrument = rows_by_instrument
        self.read_params: list[dict[str, object] | None] = []

    def query(self, q: str, params: dict[str, object] | None = None) -> _FakeQueryResult:
        if "OPTIONAL MATCH (rl:Role" in q:
            self.read_params.append(params)
            if "{id: $rid}" in q:
                assert params is not None
                return _FakeQueryResult([*self._rows_by_instrument[str(params["rid"])]])
        return super().query(q, params)


def _find_edge_calls(graph: _FakeBaselineGraph, relationship_type: str) -> list[_RecordedCall]:
    return [call for call in graph.calls if f"[:{relationship_type}]" in call.query]


def _never_called_completion(invocations: list[bool]) -> CompletionCaller:
    """A `CompletionCaller` fake that raises if it is EVER invoked --
    Increment 16 test (e)'s direct proof that the dangling-`role_id` check
    runs before any LLM call is made for any Role's Requirements.
    """

    def _call(*, model: str, messages: list[dict[str, str]], timeout: float) -> ModelResponse:
        invocations.append(True)
        raise AssertionError("call_completion should never be invoked")

    return _call


def test_derive_obligations_and_capabilities_ac003_full_flow(make_emitter: MakeEmitter) -> None:
    """(a) AC-003: 2 Roles, 3 Requirements, all matchable -> every
    Requirement has >=1 SATISFIED_BY, every Obligation has exactly 1 HAS
    from its Role, every Obligation has >=1 REQUIRES.
    """
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

    satisfied_source_ids = {call.params["source_id"] for call in satisfied_by_calls if call.params}
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


def test_derive_reads_only_the_requested_instruments_requirements(
    make_emitter: MakeEmitter,
) -> None:
    """A re-ingested amendment shares `{short}_baseline` with its prior version (#201): deriving
    the new version must not re-derive (or LLM-process) the prior version's Requirements.
    """
    emitter, _log_path = make_emitter()
    baseline_graph = _VersionedBaselineGraph(
        {
            "CRA-1.0": [
                ["CRA-1.0_req_1", "Prior duty one.", _ROLE_MANUFACTURER, _ROLE_MANUFACTURER, "M"],
                ["CRA-1.0_req_2", "Prior duty two.", _ROLE_MANUFACTURER, _ROLE_MANUFACTURER, "M"],
            ],
            "CRA-2.0": [
                ["CRA-2.0_req_1", "New duty.", _ROLE_MANUFACTURER, _ROLE_MANUFACTURER, "M"],
            ],
        }
    )
    call_completion = _scripted_sequential_call_completion(
        [_mint_response("Conduct New Duty"), _capability_mint_response("New Duty Tooling")]
    )

    result = derive_obligations_and_capabilities(
        "CRA-2.0",
        baseline_graph=baseline_graph,
        model="fake-model",
        call_completion=call_completion,
        emitter=emitter,
    )

    assert baseline_graph.read_params == [{"rid": "CRA-2.0"}]
    assert len(result.obligation_node_ids) == 1
    satisfied = {
        call.params["source_id"]
        for call in _find_edge_calls(baseline_graph, "SATISFIED_BY")
        if call.params
    }
    assert satisfied == {"CRA-2.0_req_1"}


def test_derive_obligations_and_capabilities_ac004_unmatched_requirement_surfaced(
    make_emitter: MakeEmitter, read_lines: ReadLines
) -> None:
    """(b) AC-004: one Requirement's scripted response is unmatchable -> it
    appears in DerivationResult.unmatched_requirement_ids, has no
    SATISFIED_BY edge written, and an outcome="unmatched" log entry is
    emitted naming its id.
    """
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
    satisfied_source_ids = {call.params["source_id"] for call in satisfied_by_calls if call.params}
    assert "CRA_req_art_13.9" not in satisfied_source_ids

    lines = read_lines(log_path)
    unmatched_entries = [line for line in lines if line.get("outcome") == "unmatched"]
    assert len(unmatched_entries) == 1
    assert unmatched_entries[0]["entity_id"] == "CRA_req_art_13.9"


def test_derive_obligations_and_capabilities_unmatched_obligation_surfaced(
    make_emitter: MakeEmitter,
) -> None:
    """Issue #64 / AC-BI-001, AC-BI-002, AC-BI-003 -- end-to-end integration
    proof through the public entry point (not the module-internal
    `_derive_capabilities`). 2 Requirements under one Role, both
    Obligation-derivation calls succeed (2 distinct Obligations), but the
    SECOND Obligation's capability-derivation response is malformed. The
    failure is isolated: no exception, both Obligations are still
    persisted, only the first Obligation gets a Capability/REQUIRES edge,
    and the second Obligation's id is surfaced in
    `unmatched_obligation_ids`.
    """
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
            "Report security incidents to the authority.",
            _ROLE_MANUFACTURER,
            _ROLE_MANUFACTURER,
            "Manufacturer",
        ],
    ]
    baseline_graph = _FakeBaselineGraph(rows)
    first_obligation_text = "Conduct Cybersecurity Risk Assessment"
    second_obligation_text = "Report Security Incidents"
    second_obligation_id = obligation_id(_ROLE_MANUFACTURER, second_obligation_text)
    call_completion = _scripted_sequential_call_completion(
        [
            _mint_response(first_obligation_text),
            _mint_response(second_obligation_text),
            _capability_mint_response("Risk Assessment Tooling"),
            "{not valid json",
        ]
    )

    result = derive_obligations_and_capabilities(
        "CRA-1.0",
        baseline_graph=baseline_graph,
        model="fake-model",
        call_completion=call_completion,
        emitter=emitter,
    )

    assert result.unmatched_obligation_ids == (second_obligation_id,)
    assert len(result.obligation_node_ids) == 2
    assert second_obligation_id in result.obligation_node_ids
    assert len(result.capability_node_ids) == 1

    requires_calls = _find_edge_calls(baseline_graph, "REQUIRES")
    requires_source_ids = {call.params["source_id"] for call in requires_calls if call.params}
    assert requires_source_ids == {
        oid for oid in result.obligation_node_ids if oid != second_obligation_id
    }
    assert second_obligation_id not in requires_source_ids


def test_derive_obligations_and_capabilities_ac007_emits_log_entry_with_bound_run_id(
    make_emitter: MakeEmitter, read_lines: ReadLines
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
    make_emitter: MakeEmitter,
) -> None:
    """(d) A Role with 2 Requirements, both scripted unmatchable -> both ids
    in unmatched_requirement_ids, zero Obligation nodes/HAS edges for that
    Role (permitted -- HAS is 1:0..*), no exception.
    """
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
    make_emitter: MakeEmitter,
) -> None:
    """(e) A fake baseline graph scripted so one Requirement's role_id does
    not resolve to any Role node -> raises DomainMapperDerivationError
    naming the Requirement id and the dangling role_id, BEFORE any LLM call
    is made (the structural fake for call_completion is never invoked).
    """
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


def test_derive_obligations_and_capabilities_renders_registry_with_id_name_and_description(
    make_emitter: MakeEmitter,
) -> None:
    """AC-BI-001/002: the capability call sees each prior registry entry as
    `id: name — description` (or `(no description)`) inside
    `<capability_registry>`, and a `None` description never errors.
    """
    emitter, _log_path = make_emitter()
    rows: list[list[object]] = [
        [
            "CRA_req_art_13.1",
            "Restrict access to the product.",
            _ROLE_MANUFACTURER,
            _ROLE_MANUFACTURER,
            "Manufacturer",
        ],
        [
            "CRA_req_art_13.2",
            "Record security-relevant events.",
            _ROLE_MANUFACTURER,
            _ROLE_MANUFACTURER,
            "Manufacturer",
        ],
        [
            "CRA_req_art_13.3",
            "Notify the authority of incidents.",
            _ROLE_MANUFACTURER,
            _ROLE_MANUFACTURER,
            "Manufacturer",
        ],
    ]
    baseline_graph = _FakeBaselineGraph(rows)
    access_description = "Mechanisms that restrict who can access a product or system."
    recorded_messages: list[list[dict[str, str]]] = []
    call_completion = _recording_sequential_call_completion(
        [
            _mint_response("Restrict Product Access"),
            _mint_response("Record Security Events"),
            _mint_response("Notify Authority Of Incidents"),
            _capability_mint_response("Access Control System", access_description),
            _capability_mint_response("Security Logging", None),
            _capability_mint_response("Regulatory Notification Workflow", "Notify regulators."),
        ],
        recorded_messages,
    )

    derive_obligations_and_capabilities(
        "CRA-1.0",
        baseline_graph=baseline_graph,
        model="fake-model",
        call_completion=call_completion,
        emitter=emitter,
    )

    third_capability_user_message = recorded_messages[5][1]["content"]
    assert "<capability_registry>" in third_capability_user_message
    registry_block = third_capability_user_message.split("<capability_registry>")[1].split(
        "</capability_registry>"
    )[0]
    assert (
        f"{capability_id('Access Control System')}: Access Control System — {access_description}"
        in registry_block
    )
    assert (
        f"{capability_id('Security Logging')}: Security Logging — (no description)"
        in registry_block
    )
    assert len(_find_edge_calls(baseline_graph, "REQUIRES")) == 3


# ---------------------------------------------------------------------------
# #187: per-reuse verification (accept path). Driven through the public entry
# point with `_prompt_routed_call_completion` and `_FakeBaselineGraph`; one
# Requirement per Obligation, all under `_ROLE_MANUFACTURER`, minted in
# document order, so Capability calls happen in that same order (A, B, ...).

_OBLIGATION_TEXT_A = "Restrict Access To The Product"
_OBLIGATION_TEXT_B = "Maintain Organisational Structure For Continuity"
_ACCESS_CONTROL_NAME = "Access Control System"
_ACCESS_CONTROL_DESCRIPTION = "Controls access."
_ACCESS_CONTROL_ID = capability_id(_ACCESS_CONTROL_NAME)


def _manufacturer_requirement_rows(count: int) -> list[list[object]]:
    return [
        [
            f"CRA_req_art_20.{index}",
            f"Requirement duty number {index}.",
            _ROLE_MANUFACTURER,
            _ROLE_MANUFACTURER,
            "Manufacturer",
        ]
        for index in range(1, count + 1)
    ]


def _derive_through_entry_point(
    *,
    obligation_texts: list[str],
    capability: list[str],
    verification: list[str | Exception],
    emitter: LogEmitter,
) -> tuple[DerivationResult, _FakeBaselineGraph, _RecordedByPrompt, list[str]]:
    baseline_graph = _FakeBaselineGraph(_manufacturer_requirement_rows(len(obligation_texts)))
    call_completion, recorded = _prompt_routed_call_completion(
        obligation=[_mint_response(text) for text in obligation_texts],
        capability=capability,
        verification=verification,
    )
    result = derive_obligations_and_capabilities(
        "CRA-1.0",
        baseline_graph=baseline_graph,
        model="fake-model",
        call_completion=call_completion,
        emitter=emitter,
    )
    obligation_ids = [obligation_id(_ROLE_MANUFACTURER, text) for text in obligation_texts]
    return result, baseline_graph, recorded, obligation_ids


def _requires_pairs(graph: _FakeBaselineGraph) -> set[tuple[object, object]]:
    return {
        (call.params["source_id"], call.params["target_id"])
        for call in _find_edge_calls(graph, "REQUIRES")
        if call.params
    }


def _requires_targets_from(graph: _FakeBaselineGraph, source_id: str) -> set[object]:
    return {target for source, target in _requires_pairs(graph) if source == source_id}


def _reuse_scenario(
    emitter: LogEmitter,
) -> tuple[DerivationResult, _FakeBaselineGraph, _RecordedByPrompt, list[str]]:
    """A mints X ("Access Control System"); B proposes reuse of X; accept."""
    return _derive_through_entry_point(
        obligation_texts=[_OBLIGATION_TEXT_A, _OBLIGATION_TEXT_B],
        capability=[
            _capability_mint_response(_ACCESS_CONTROL_NAME, _ACCESS_CONTROL_DESCRIPTION),
            _capability_match_response(_ACCESS_CONTROL_ID),
        ],
        verification=[_reuse_accept_response()],
        emitter=emitter,
    )


def test_derive_obligations_and_capabilities_reuse_proposal_makes_exactly_one_verification_call(
    make_emitter: MakeEmitter,
) -> None:
    """#187 AC-BI-003: B's reuse proposal triggers exactly one verification
    call; A's first mint triggers none (the one call carries B's duty text).
    """
    emitter, _log_path = make_emitter()

    _result, _graph, recorded, _ids = _reuse_scenario(emitter)

    assert len(recorded["capability"]) == 2
    assert len(recorded["verification"]) == 1
    verification_user_content = recorded["verification"][0][1]["content"]
    assert _OBLIGATION_TEXT_B in verification_user_content
    assert _OBLIGATION_TEXT_A not in verification_user_content


def test_derive_obligations_and_capabilities_mint_proposals_make_no_verification_call(
    make_emitter: MakeEmitter,
) -> None:
    """#187 AC-BI-003 (mint half, A-M1 rewrite): two first mints of distinct
    names are not reuses, so no verification call is made.
    """
    emitter, _log_path = make_emitter()
    security_logging_id = capability_id("Security Logging")

    result, graph, recorded, (id_a, id_b) = _derive_through_entry_point(
        obligation_texts=[_OBLIGATION_TEXT_A, _OBLIGATION_TEXT_B],
        capability=[
            _capability_mint_response(_ACCESS_CONTROL_NAME, _ACCESS_CONTROL_DESCRIPTION),
            _capability_mint_response("Security Logging", "Logs security events."),
        ],
        verification=[],
        emitter=emitter,
    )

    assert len(recorded["verification"]) == 0
    assert result.capability_node_ids == (_ACCESS_CONTROL_ID, security_logging_id)
    assert _requires_pairs(graph) == {(id_a, _ACCESS_CONTROL_ID), (id_b, security_logging_id)}
    assert result.unmatched_obligation_ids == ()


@pytest.mark.parametrize(
    "b_name",
    [
        pytest.param("Internal Controls", id="exact_name"),
        pytest.param("internal controls", id="case_variant"),
    ],
)
def test_derive_obligations_and_capabilities_mint_of_existing_capability_name_is_verified_as_reuse(
    b_name: str, make_emitter: MakeEmitter, read_lines: ReadLines
) -> None:
    """#187 A-M1: B's "mint" of a name already in the registry (exactly or
    case-insensitively -- `capability_id` lower-cases) attaches to the
    existing Capability, so it is verified exactly like a reuse, against the
    registry's own name and description.
    """
    emitter, log_path = make_emitter()
    existing_description = "Maintains the internal control framework."
    internal_controls_id = capability_id("Internal Controls")

    result, graph, recorded, (_id_a, id_b) = _derive_through_entry_point(
        obligation_texts=[_OBLIGATION_TEXT_A, _OBLIGATION_TEXT_B],
        capability=[
            _capability_mint_response("Internal Controls", existing_description),
            _capability_mint_response(b_name, "Something else."),
        ],
        verification=[_reuse_accept_response()],
        emitter=emitter,
    )
    emitter.flush()

    assert len(recorded["verification"]) == 1
    verification_user_content = recorded["verification"][0][1]["content"]
    assert "<capability_name>Internal Controls</capability_name>" in verification_user_content
    description_block = verification_user_content.split("<capability_description>")[1].split(
        "</capability_description>"
    )[0]
    assert existing_description in description_block
    assert result.capability_node_ids == (internal_controls_id,)
    assert _requires_targets_from(graph, id_b) == {internal_controls_id}
    verdict_lines = [
        line for line in read_lines(log_path) if line.get("action") == "verify_capability_reuse"
    ]
    assert len(verdict_lines) == 1
    assert verdict_lines[0]["entity_id"] == [id_b, internal_controls_id]
    assert verdict_lines[0]["outcome"] == "reuse_accepted"


def test_derive_obligations_and_capabilities_accepted_reuse_keeps_existing_capability(
    make_emitter: MakeEmitter,
) -> None:
    """#187 AC-BI-004: an accepted reuse attaches B to the existing
    Capability X and mints nothing new.
    """
    emitter, _log_path = make_emitter()

    result, graph, _recorded, (_id_a, id_b) = _reuse_scenario(emitter)

    assert result.capability_node_ids == (_ACCESS_CONTROL_ID,)
    assert (id_b, _ACCESS_CONTROL_ID) in _requires_pairs(graph)
    assert result.unmatched_obligation_ids == ()


def test_derive_capabilities_accepted_reuse_keeps_existing_capability_node(
    make_emitter: MakeEmitter,
) -> None:
    """#187 AC-BI-004 at `_derive_capabilities` level: B's edge targets X
    and the run still holds exactly one Capability node.
    """
    emitter, _log_path = make_emitter()
    call_completion, _recorded = _prompt_routed_call_completion(
        obligation=[],
        capability=[
            _capability_mint_response(_ACCESS_CONTROL_NAME, _ACCESS_CONTROL_DESCRIPTION),
            _capability_match_response(_ACCESS_CONTROL_ID),
        ],
        verification=[_reuse_accept_response()],
    )

    capability_nodes, requires_edges, _unmatched = _derive_capabilities(
        (_OBLIGATION_A, _OBLIGATION_B),
        model="fake-model",
        call_completion=call_completion,
        emitter=emitter,
    )

    assert len(capability_nodes) == 1
    edges_from_b = [e for e in requires_edges if e.obligation_node_id == _OBLIGATION_B.id]
    assert [e.capability_node_id for e in edges_from_b] == [_ACCESS_CONTROL_ID]


def test_reuse_verification_messages_delimit_obligation_text_and_description_as_untrusted(
    make_emitter: MakeEmitter,
) -> None:
    """#187 AC-BI-007: the verification call carries the Obligation text and
    the Capability's name/description only as tag-delimited user content,
    never in the system prompt.
    """
    emitter, _log_path = make_emitter()

    _result, _graph, recorded, _ids = _reuse_scenario(emitter)

    system_message, user_message = recorded["verification"][0]
    assert system_message["role"] == "system"
    assert system_message["content"] == CAPABILITY_REUSE_VERIFICATION_SYSTEM_PROMPT
    user_content = user_message["content"]
    assert f"<obligation_text>\n{_OBLIGATION_TEXT_B}\n</obligation_text>" in user_content
    assert f"<capability_name>{_ACCESS_CONTROL_NAME}</capability_name>" in user_content
    assert (
        f"<capability_description>{_ACCESS_CONTROL_DESCRIPTION}</capability_description>"
        in user_content
    )
    assert _OBLIGATION_TEXT_B not in system_message["content"]
    assert _ACCESS_CONTROL_DESCRIPTION not in system_message["content"]


def test_derive_obligations_and_capabilities_logs_accepted_reuse_verdict(
    make_emitter: MakeEmitter, read_lines: ReadLines
) -> None:
    """#187 AC-BI-011 (accept half): exactly one verdict line, keyed by
    (B, X), outcome "reuse_accepted", with no reject-only keys.
    """
    emitter, log_path = make_emitter()

    _result, _graph, _recorded, (_id_a, id_b) = _reuse_scenario(emitter)
    emitter.flush()

    verdict_lines = [
        line for line in read_lines(log_path) if line.get("action") == "verify_capability_reuse"
    ]
    assert len(verdict_lines) == 1
    line = verdict_lines[0]
    assert line["component"] == "domain_mapper"
    assert line["entity_id"] == [id_b, _ACCESS_CONTROL_ID]
    assert line["outcome"] == "reuse_accepted"
    assert "minted_capability_id" not in line
    assert "extra" not in line


# ---------------------------------------------------------------------------
# #191: recalibrated reuse verification (accept path), real #187 DORA cases.

_EU_REPORTING_NAME = "EU Regulatory Reporting"
_EU_REPORTING_DESCRIPTION = (
    "Ability to submit required legal or regulatory notifications and reports to EU "
    "authorities in compliance with applicable rules."
)
_EU_REPORTING_ID = capability_id(_EU_REPORTING_NAME)
_EU_LAWS_OBLIGATION_TEXT = "Notify Implementing Laws and Provisions to EU Authorities"
_ICT_INCIDENT_OBLIGATION_TEXT = "Report Major ICT-Related Incidents to the Competent Authority"
_SUBSUMPTION_GUIDANCE = "A broader description can cover a narrower duty"


def test_derive_obligations_and_capabilities_accepts_broader_eu_regulatory_reporting_for_ict_incident_reporting_duty(  # noqa: E501
    make_emitter: MakeEmitter, read_lines: ReadLines
) -> None:
    """#191 AC-BI-001/AC-BI-003: the #187 DORA subsumption case
    (dora-reingest-187.jsonl:1053). B's narrower ICT-incident reporting duty
    is proposed for reuse of the broader "EU Regulatory Reporting"; the
    verifier accepts, so B attaches to it and "Incident Reporting" is never
    minted. Q7: A's mint of EU Regulatory Reporting is first-pass here,
    whereas in the real run it came from a verifier mint; that is irrelevant
    to B's decision.

    The red step proves only that the recalibrated guidance is the system
    prompt recorded at the LLM boundary (implied by the prompt pin, since the
    fake routes on identity); the verdict is scripted, so behavioural proof
    that a real model now accepts lives in test_reuse_verification_live.py (S4).
    """
    emitter, log_path = make_emitter()

    result, graph, recorded, (id_a, id_b) = _derive_through_entry_point(
        obligation_texts=[_EU_LAWS_OBLIGATION_TEXT, _ICT_INCIDENT_OBLIGATION_TEXT],
        capability=[
            _capability_mint_response(_EU_REPORTING_NAME, _EU_REPORTING_DESCRIPTION),
            _capability_match_response(_EU_REPORTING_ID),
        ],
        verification=[_reuse_accept_response()],
        emitter=emitter,
    )
    emitter.flush()

    assert len(recorded["verification"]) == 1
    system_message, user_message = recorded["verification"][0]
    assert _SUBSUMPTION_GUIDANCE in system_message["content"]
    assert user_message["content"] == (
        f"<obligation_text>\n{_ICT_INCIDENT_OBLIGATION_TEXT}\n</obligation_text>\n\n"
        f"<capability_name>{_EU_REPORTING_NAME}</capability_name>\n"
        f"<capability_description>{_EU_REPORTING_DESCRIPTION}</capability_description>"
    )
    verdict_lines = [
        line for line in read_lines(log_path) if line.get("action") == "verify_capability_reuse"
    ]
    assert len(verdict_lines) == 1
    assert verdict_lines[0]["entity_id"] == [id_b, _EU_REPORTING_ID]
    assert verdict_lines[0]["outcome"] == "reuse_accepted"
    assert "minted_capability_id" not in verdict_lines[0]
    assert result.capability_node_ids == (_EU_REPORTING_ID,)
    assert capability_id("Incident Reporting") not in _capability_node_writes(graph)
    assert _requires_targets_from(graph, id_b) == {_EU_REPORTING_ID}
    assert _requires_pairs(graph) == {(id_a, _EU_REPORTING_ID), (id_b, _EU_REPORTING_ID)}
    assert result.unmatched_obligation_ids == ()


_ICT_OP_RISK_NAME = "ICT Operational Risk Management"
_ICT_OP_RISK_DESCRIPTION = (
    "Ability to identify, assess, monitor, and control operational risks arising from "
    "ICT systems, processes, and services."
)
_ICT_OP_RISK_ID = capability_id(_ICT_OP_RISK_NAME)
_ICT_EXPERTISE_OBLIGATION_TEXT = "Have Expertise in ICT Matters and Operational Risk"
_OP_RISK_OBLIGATION_TEXT = (
    "Identify and Minimise Operational Risks Through Appropriate Systems, Controls, and Procedures"
)
_DOMAIN_QUALIFIER_GUIDANCE = "A domain qualifier alone does not make two capacities distinct"


def test_derive_obligations_and_capabilities_accepts_ict_operational_risk_management_for_unqualified_operational_risk_duty(  # noqa: E501
    make_emitter: MakeEmitter, read_lines: ReadLines
) -> None:
    """#191 AC-BI-002/AC-BI-004: the #187 DORA domain-qualifier case
    (dora-reingest-187.jsonl:1904). B's unqualified operational-risk duty is
    proposed for reuse of the existing, ICT-scoped "ICT Operational Risk
    Management"; the verifier accepts, so B attaches to it and "Operational
    Risk Management" is never minted. Direction follows the raw #187 log
    (PLAN.md G4), not the issue prose, per user decision C1(b), CHANGES.md.

    The red step proves only that the recalibrated guidance is the system
    prompt recorded at the LLM boundary (implied by the prompt pin, since the
    fake routes on identity); the verdict is scripted, so behavioural proof
    that a real model now accepts lives in test_reuse_verification_live.py (S4).
    """
    emitter, log_path = make_emitter()

    result, graph, recorded, (id_a, id_b) = _derive_through_entry_point(
        obligation_texts=[_ICT_EXPERTISE_OBLIGATION_TEXT, _OP_RISK_OBLIGATION_TEXT],
        capability=[
            _capability_mint_response(_ICT_OP_RISK_NAME, _ICT_OP_RISK_DESCRIPTION),
            _capability_match_response(_ICT_OP_RISK_ID),
        ],
        verification=[_reuse_accept_response()],
        emitter=emitter,
    )
    emitter.flush()

    assert len(recorded["verification"]) == 1
    system_message, user_message = recorded["verification"][0]
    assert _DOMAIN_QUALIFIER_GUIDANCE in system_message["content"]
    assert user_message["content"] == (
        f"<obligation_text>\n{_OP_RISK_OBLIGATION_TEXT}\n</obligation_text>\n\n"
        f"<capability_name>{_ICT_OP_RISK_NAME}</capability_name>\n"
        f"<capability_description>{_ICT_OP_RISK_DESCRIPTION}</capability_description>"
    )
    verdict_lines = [
        line for line in read_lines(log_path) if line.get("action") == "verify_capability_reuse"
    ]
    assert len(verdict_lines) == 1
    assert verdict_lines[0]["entity_id"] == [id_b, _ICT_OP_RISK_ID]
    assert verdict_lines[0]["outcome"] == "reuse_accepted"
    assert "minted_capability_id" not in verdict_lines[0]
    assert result.capability_node_ids == (_ICT_OP_RISK_ID,)
    assert capability_id("Operational Risk Management") not in _capability_node_writes(graph)
    assert _requires_targets_from(graph, id_b) == {_ICT_OP_RISK_ID}
    assert _requires_pairs(graph) == {(id_a, _ICT_OP_RISK_ID), (id_b, _ICT_OP_RISK_ID)}
    assert result.unmatched_obligation_ids == ()


# ---------------------------------------------------------------------------
# #187: per-reuse verification (reject path).

_CONTINUITY_NAME = "Organisational Continuity Structure"
_CONTINUITY_DESCRIPTION = "Maintains the organisational structure needed for continuity."
_CONTINUITY_ID = capability_id(_CONTINUITY_NAME)


def _reuse_reject_response(
    new_name: str, new_description: str = "Covers the duty.", confidence: float = 0.8
) -> str:
    return json.dumps(
        {
            "verdict": "reject",
            "new_name": new_name,
            "new_description": new_description,
            "confidence": confidence,
        }
    )


def _rejected_reuse_scenario(
    emitter: LogEmitter,
) -> tuple[DerivationResult, _FakeBaselineGraph, _RecordedByPrompt, list[str]]:
    """A mints X ("Access Control System"); B proposes reuse of X; the
    verifier rejects and names "Organisational Continuity Structure".
    """
    return _derive_through_entry_point(
        obligation_texts=[_OBLIGATION_TEXT_A, _OBLIGATION_TEXT_B],
        capability=[
            _capability_mint_response(_ACCESS_CONTROL_NAME, _ACCESS_CONTROL_DESCRIPTION),
            _capability_match_response(_ACCESS_CONTROL_ID),
        ],
        verification=[_reuse_reject_response(_CONTINUITY_NAME, _CONTINUITY_DESCRIPTION)],
        emitter=emitter,
    )


def _capability_node_writes(graph: _FakeBaselineGraph) -> dict[object, object]:
    return {
        call.params["id"]: call.params["properties"]
        for call in graph.calls
        if call.params and "MERGE (n:Capability" in call.query
    }


def test_derive_obligations_and_capabilities_rejected_reuse_mints_verifier_capability(
    make_emitter: MakeEmitter,
) -> None:
    """#187 AC-BI-005: a rejected reuse mints the verifier's more specific
    Capability (its description, status "active") and attaches B to it --
    never to X -- while A keeps its edge to X.
    """
    emitter, _log_path = make_emitter()

    result, graph, _recorded, (id_a, id_b) = _rejected_reuse_scenario(emitter)

    minted_properties = _capability_node_writes(graph)[_CONTINUITY_ID]
    assert isinstance(minted_properties, dict)
    assert minted_properties["description"] == _CONTINUITY_DESCRIPTION
    assert minted_properties["status"] == "active"
    assert _requires_targets_from(graph, id_b) == {_CONTINUITY_ID}
    assert (id_a, _ACCESS_CONTROL_ID) in _requires_pairs(graph)
    assert result.capability_node_ids == (_ACCESS_CONTROL_ID, _CONTINUITY_ID)
    assert result.unmatched_obligation_ids == ()


def test_derive_obligations_and_capabilities_logs_rejected_reuse_verdict(
    make_emitter: MakeEmitter, read_lines: ReadLines
) -> None:
    """#187 AC-BI-011 (reject half): exactly one verdict line, keyed by
    (B, X), outcome "reuse_rejected", carrying the minted id as a top-level
    key (A-m5: `extra` is flattened, never nested).
    """
    emitter, log_path = make_emitter()

    _result, _graph, _recorded, (_id_a, id_b) = _rejected_reuse_scenario(emitter)
    emitter.flush()

    verdict_lines = [
        line for line in read_lines(log_path) if line.get("action") == "verify_capability_reuse"
    ]
    assert len(verdict_lines) == 1
    line = verdict_lines[0]
    assert line["entity_id"] == [id_b, _ACCESS_CONTROL_ID]
    assert line["outcome"] == "reuse_rejected"
    assert line["minted_capability_id"] == _CONTINUITY_ID
    assert "extra" not in line


def test_derive_obligations_and_capabilities_mint_of_existing_capability_name_rejected_mints_verifier_capability(  # noqa: E501
    make_emitter: MakeEmitter,
) -> None:
    """#187 A-M1: B's "mint" of a name already in the registry is verified
    as a reuse; on reject, B attaches to the verifier's Capability instead
    of X, and A keeps X.
    """
    emitter, _log_path = make_emitter()
    internal_controls_id = capability_id("Internal Controls")
    liquidity_id = capability_id("Liquidity Control Procedures")

    result, graph, _recorded, (id_a, id_b) = _derive_through_entry_point(
        obligation_texts=[_OBLIGATION_TEXT_A, _OBLIGATION_TEXT_B],
        capability=[
            _capability_mint_response(
                "Internal Controls", "Maintains the internal control framework."
            ),
            _capability_mint_response("Internal Controls", "Something else."),
        ],
        verification=[
            _reuse_reject_response("Liquidity Control Procedures", "Controls liquidity risk.")
        ],
        emitter=emitter,
    )

    assert _requires_targets_from(graph, id_b) == {liquidity_id}
    assert (id_a, internal_controls_id) in _requires_pairs(graph)
    assert result.capability_node_ids == (internal_controls_id, liquidity_id)


_OBLIGATION_TEXT_C = "Review Privileged Access And Retain Audit Trails"
_SECURITY_LOGGING_NAME = "Security Logging"
_SECURITY_LOGGING_ID = capability_id(_SECURITY_LOGGING_NAME)


@pytest.mark.parametrize(
    ("verification", "rejected_id", "expected_c_targets"),
    [
        pytest.param(
            [_reuse_reject_response("Privileged Access Review"), _reuse_accept_response()],
            _ACCESS_CONTROL_ID,
            {capability_id("Privileged Access Review"), _SECURITY_LOGGING_ID},
            id="reject_first",
        ),
        pytest.param(
            [_reuse_accept_response(), _reuse_reject_response("Audit Trail Retention")],
            _SECURITY_LOGGING_ID,
            {_ACCESS_CONTROL_ID, capability_id("Audit Trail Retention")},
            id="reject_second",
        ),
    ],
)
def test_derive_obligations_and_capabilities_multiple_reuses_in_one_response_are_verified_independently(  # noqa: E501
    verification: list[str | Exception],
    rejected_id: str,
    expected_c_targets: set[object],
    make_emitter: MakeEmitter,
) -> None:
    """#187 AC-BI-006: each reuse in one response gets its own verification
    call, and each verdict decides only its own proposal.
    """
    emitter, _log_path = make_emitter()
    reuse_both = json.dumps(
        {
            "capabilities": [
                {
                    "matched_existing_id": _ACCESS_CONTROL_ID,
                    "new_name": None,
                    "new_description": None,
                    "confidence": 0.9,
                },
                {
                    "matched_existing_id": _SECURITY_LOGGING_ID,
                    "new_name": None,
                    "new_description": None,
                    "confidence": 0.9,
                },
            ]
        }
    )

    result, graph, recorded, (_id_a, _id_b, id_c) = _derive_through_entry_point(
        obligation_texts=[_OBLIGATION_TEXT_A, _OBLIGATION_TEXT_B, _OBLIGATION_TEXT_C],
        capability=[
            _capability_mint_response(_ACCESS_CONTROL_NAME, _ACCESS_CONTROL_DESCRIPTION),
            _capability_mint_response(_SECURITY_LOGGING_NAME, "Logs security events."),
            reuse_both,
        ],
        verification=verification,
        emitter=emitter,
    )

    assert len(recorded["verification"]) == 2
    c_targets = {
        call.params["target_id"]
        for call in _find_edge_calls(graph, "REQUIRES")
        if call.params and call.params["source_id"] == id_c
    }
    assert c_targets == expected_c_targets
    assert rejected_id not in c_targets
    assert result.unmatched_obligation_ids == ()


_OBLIGATION_TEXT_D = "Log Security Relevant Events"
_INCIDENT_REPORTING_ID = capability_id("Incident Reporting Workflow")


@pytest.mark.parametrize(
    "new_name",
    [
        pytest.param("Security Logging", id="exact_name"),
        pytest.param("security logging", id="case_variant"),
    ],
)
def test_derive_obligations_and_capabilities_verifier_minted_name_colliding_with_registry_marks_unmatched(  # noqa: E501
    new_name: str, make_emitter: MakeEmitter, read_lines: ReadLines
) -> None:
    """#187 AC-BI-009: a verifier "mint" whose id is already in the registry
    (exactly or case-insensitively; `capability_id` does not normalise
    whitespace, so there is no whitespace case) marks B unmatched -- no
    retry, no second verification call -- and the run continues with C.
    """
    emitter, log_path = make_emitter()

    result, graph, recorded, (_id_a, _id_d, id_b, id_c) = _derive_through_entry_point(
        obligation_texts=[
            _OBLIGATION_TEXT_A,
            _OBLIGATION_TEXT_D,
            _OBLIGATION_TEXT_B,
            _OBLIGATION_TEXT_C,
        ],
        capability=[
            _capability_mint_response(_ACCESS_CONTROL_NAME, _ACCESS_CONTROL_DESCRIPTION),
            _capability_mint_response(_SECURITY_LOGGING_NAME, "Logs security events."),
            _capability_match_response(_ACCESS_CONTROL_ID),
            _capability_mint_response("Incident Reporting Workflow", "Reports incidents."),
        ],
        verification=[_reuse_reject_response(new_name)],
        emitter=emitter,
    )
    emitter.flush()

    assert len(recorded["verification"]) == 1
    assert result.unmatched_obligation_ids == (id_b,)
    assert _requires_targets_from(graph, id_b) == set()
    assert (id_c, _INCIDENT_REPORTING_ID) in _requires_pairs(graph)
    lines = read_lines(log_path)
    unmatched_lines = [line for line in lines if line.get("outcome") == "unmatched"]
    assert len(unmatched_lines) == 1
    assert unmatched_lines[0]["entity_id"] == id_b
    verdict_lines = [line for line in lines if line.get("action") == "verify_capability_reuse"]
    assert len(verdict_lines) == 1
    verdict_line = verdict_lines[0]
    assert verdict_line["outcome"] == "reuse_rejected"
    assert verdict_line["collision"] is True
    assert verdict_line["proposed_capability_id"] == _SECURITY_LOGGING_ID
    assert "minted_capability_id" not in verdict_line


def test_derive_obligations_and_capabilities_verifier_mint_equal_to_same_response_mint_converges_on_one_capability(  # noqa: E501
    make_emitter: MakeEmitter,
) -> None:
    """#187 A-m6: when the verifier mints the same Capability as another mint
    in B's own response, the run holds that Capability once and B attaches
    only to it (a duplicate REQUIRES entry is harmless -- the writer MERGEs).
    """
    emitter, _log_path = make_emitter()
    mint_and_reuse = json.dumps(
        {
            "capabilities": [
                {
                    "matched_existing_id": None,
                    "new_name": _CONTINUITY_NAME,
                    "new_description": "Maintains continuity structure.",
                    "confidence": 0.9,
                },
                {
                    "matched_existing_id": _ACCESS_CONTROL_ID,
                    "new_name": None,
                    "new_description": None,
                    "confidence": 0.9,
                },
            ]
        }
    )

    result, graph, _recorded, (_id_a, id_b) = _derive_through_entry_point(
        obligation_texts=[_OBLIGATION_TEXT_A, _OBLIGATION_TEXT_B],
        capability=[
            _capability_mint_response(_ACCESS_CONTROL_NAME, _ACCESS_CONTROL_DESCRIPTION),
            mint_and_reuse,
        ],
        verification=[_reuse_reject_response(_CONTINUITY_NAME)],
        emitter=emitter,
    )

    assert result.capability_node_ids.count(_CONTINUITY_ID) == 1
    assert _requires_targets_from(graph, id_b) == {_CONTINUITY_ID}
    assert result.unmatched_obligation_ids == ()


# ---------------------------------------------------------------------------
# #187: malformed verification responses (AC-BI-008, characterisation pins --
# the phase-1 catch and two-phase atomicity landed with S2/S3).


@pytest.mark.parametrize(
    "malformed_verification",
    [
        pytest.param("{not valid json", id="invalid_json"),
        pytest.param(
            json.dumps({"new_name": "Organisational Continuity Structure", "confidence": 0.8}),
            id="missing_verdict",
        ),
        pytest.param(
            json.dumps(
                {
                    "verdict": "reject",
                    "new_name": None,
                    "new_description": "Covers the duty.",
                    "confidence": 0.8,
                }
            ),
            id="reject_without_new_name",
        ),
        pytest.param(json.dumps({"verdict": "maybe"}), id="unknown_verdict"),
    ],
)
def test_derive_obligations_and_capabilities_malformed_reuse_verification_marks_obligation_unmatched(  # noqa: E501
    malformed_verification: str, make_emitter: MakeEmitter, read_lines: ReadLines
) -> None:
    """#187 AC-BI-008: a malformed verification response for B's reuse marks
    B unmatched with no REQUIRES edge, and the run continues with C.
    """
    emitter, log_path = make_emitter()

    result, graph, _recorded, (_id_a, id_b, id_c) = _derive_through_entry_point(
        obligation_texts=[_OBLIGATION_TEXT_A, _OBLIGATION_TEXT_B, _OBLIGATION_TEXT_C],
        capability=[
            _capability_mint_response(_ACCESS_CONTROL_NAME, _ACCESS_CONTROL_DESCRIPTION),
            _capability_match_response(_ACCESS_CONTROL_ID),
            _capability_mint_response("Incident Reporting Workflow", "Reports incidents."),
        ],
        verification=[malformed_verification],
        emitter=emitter,
    )
    emitter.flush()

    assert result.unmatched_obligation_ids == (id_b,)
    assert _requires_targets_from(graph, id_b) == set()
    assert (id_c, _INCIDENT_REPORTING_ID) in _requires_pairs(graph)
    unmatched_lines = [line for line in read_lines(log_path) if line.get("outcome") == "unmatched"]
    assert len(unmatched_lines) == 1
    assert unmatched_lines[0]["entity_id"] == id_b
    assert unmatched_lines[0]["action"] == "derive_obligations_and_capabilities"


def test_derive_capabilities_malformed_second_reuse_verification_drops_all_edges_for_that_obligation(  # noqa: E501
    make_emitter: MakeEmitter,
) -> None:
    """#187 AC-BI-008 atomicity guard: C's first reuse is accepted but its
    second verification is malformed, so C gets no REQUIRES edge at all,
    and A's and B's Capabilities and edges are unchanged.
    """
    emitter, _log_path = make_emitter()
    reuse_both = json.dumps(
        {
            "capabilities": [
                {
                    "matched_existing_id": _ACCESS_CONTROL_ID,
                    "new_name": None,
                    "new_description": None,
                    "confidence": 0.9,
                },
                {
                    "matched_existing_id": _SECURITY_LOGGING_ID,
                    "new_name": None,
                    "new_description": None,
                    "confidence": 0.9,
                },
            ]
        }
    )

    result, graph, _recorded, (id_a, id_b, id_c) = _derive_through_entry_point(
        obligation_texts=[_OBLIGATION_TEXT_A, _OBLIGATION_TEXT_B, _OBLIGATION_TEXT_C],
        capability=[
            _capability_mint_response(_ACCESS_CONTROL_NAME, _ACCESS_CONTROL_DESCRIPTION),
            _capability_mint_response(_SECURITY_LOGGING_NAME, "Logs security events."),
            reuse_both,
        ],
        verification=[_reuse_accept_response(), "{not valid json"],
        emitter=emitter,
    )

    assert result.unmatched_obligation_ids == (id_c,)
    assert _requires_targets_from(graph, id_c) == set()
    assert _requires_pairs(graph) == {(id_a, _ACCESS_CONTROL_ID), (id_b, _SECURITY_LOGGING_ID)}
    assert result.capability_node_ids == (_ACCESS_CONTROL_ID, _SECURITY_LOGGING_ID)


# ---------------------------------------------------------------------------
# #187: provider failure during verification (AC-BI-010, characterisation pin).


@pytest.mark.parametrize(
    "verification_failure",
    [
        pytest.param(
            openai.APIConnectionError(request=httpx.Request("POST", "https://example.invalid")),
            id="api_connection_error",
        ),
        pytest.param("", id="empty_completion_content"),
    ],
)
def test_derive_obligations_and_capabilities_reuse_verification_provider_error_aborts_run(
    verification_failure: str | Exception, make_emitter: MakeEmitter
) -> None:
    """#187 AC-BI-010: an `LlmProviderError` from a verification call is
    never caught -- it aborts the whole run before persistence, so no
    REQUIRES/HAS/SATISFIED_BY edge is written.
    """
    emitter, _log_path = make_emitter()
    baseline_graph = _FakeBaselineGraph(_manufacturer_requirement_rows(2))
    call_completion, _recorded = _prompt_routed_call_completion(
        obligation=[_mint_response(_OBLIGATION_TEXT_A), _mint_response(_OBLIGATION_TEXT_B)],
        capability=[
            _capability_mint_response(_ACCESS_CONTROL_NAME, _ACCESS_CONTROL_DESCRIPTION),
            _capability_match_response(_ACCESS_CONTROL_ID),
        ],
        verification=[verification_failure],
    )

    with pytest.raises(LlmProviderError):
        derive_obligations_and_capabilities(
            "CRA-1.0",
            baseline_graph=baseline_graph,
            model="fake-model",
            call_completion=call_completion,
            emitter=emitter,
        )

    for relationship_type in ("REQUIRES", "HAS", "SATISFIED_BY"):
        assert _find_edge_calls(baseline_graph, relationship_type) == []
