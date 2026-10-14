"""Derivation writes its Obligation/Capability graph through the Graph Write Gateway.

The stage reads the Requirements extraction left in the baseline graph, derives with a scripted
LLM and submits ONE group on the baseline graph: Obligations, Capabilities, then the `HAS`,
`SATISFIED_BY` and `REQUIRES` edges. Role and Requirement endpoints are outside the group, so
the builder resolves them with one batched existing-node read. The baseline rig is the real
boundary (gateway over in-memory log and graph); only the LLM is faked.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest
import redis.exceptions
from litellm.types.utils import Choices, Message, ModelResponse

from domain_mapper._gateway_fakes import entry_properties, seed_extraction_output
from ps_service.domain_mapper.derivation import derive_obligations_and_capabilities
from ps_service.domain_mapper.errors import DomainMapperPersistenceError
from ps_service.domain_mapper.graph_writer import persist_obligation_and_capability_graph
from ps_service.domain_mapper.identity import capability_id, obligation_id
from ps_service.domain_mapper.models import (
    CapabilityNode,
    CapabilityRequiresEdge,
    ObligationHasEdge,
    ObligationNode,
    RequirementSatisfiedByEdge,
)
from ps_service.graph_gateway.digest import canonical_digest

if TYPE_CHECKING:
    from collections.abc import Callable

    from domain_mapper._fakes import MakeEmitter, ReadLines
    from domain_mapper._gateway_fakes import BaselineRig
    from ps_service.llm_interface.client import CompletionCaller

_INSTRUMENT_ID = "CRA-1.0"
_ROLE_MANUFACTURER = "role_manufacturer_abc123"
_ROLE_IMPORTER = "role_importer_def456"
_REQ_MANUFACTURER = "CRA-1.0_req_art_13.1"
_REQ_IMPORTER = "CRA-1.0_req_art_14.1"
_OBLIGATION_TEXT_MANUFACTURER = "Conduct Cybersecurity Risk Assessment"
_OBLIGATION_TEXT_IMPORTER = "Verify Conformity Assessment"
_CAPABILITY_NAME_MANUFACTURER = "Risk Assessment Tooling"
_CAPABILITY_NAME_IMPORTER = "Conformity Verification System"


def _response(content: str) -> ModelResponse:
    return ModelResponse(
        id="x",
        model="fake-model",
        choices=[
            Choices(
                finish_reason="stop", index=0, message=Message(content=content, role="assistant")
            )
        ],
    )


def _obligation_mint(new_text: str) -> str:
    return json.dumps(
        {"matched_existing_id": None, "new_text": new_text, "unmatchable": False, "confidence": 0.9}
    )


def _capability_mint(new_name: str) -> str:
    return json.dumps(
        {
            "capabilities": [
                {
                    "matched_existing_id": None,
                    "new_name": new_name,
                    "new_description": None,
                    "confidence": 0.9,
                }
            ]
        }
    )


def _scripted(responses: list[str]) -> CompletionCaller:
    """Return `responses` in call order; an unscripted extra call fails loudly."""
    remaining = list(responses)

    def _call(*, model: str, messages: list[dict[str, str]], timeout: float) -> ModelResponse:
        if not remaining:
            raise AssertionError("no more scripted responses -- unexpected extra LLM call")
        return _response(remaining.pop(0))

    return _call


def _seed_two_roles(rig: BaselineRig) -> None:
    seed_extraction_output(
        rig,
        _INSTRUMENT_ID,
        {_ROLE_MANUFACTURER: "Manufacturer", _ROLE_IMPORTER: "Importer"},
        [
            (_REQ_MANUFACTURER, "Conduct a cybersecurity risk assessment.", _ROLE_MANUFACTURER),
            (_REQ_IMPORTER, "Verify the manufacturer's conformity assessment.", _ROLE_IMPORTER),
        ],
    )


def _two_role_script() -> CompletionCaller:
    return _scripted(
        [
            _obligation_mint(_OBLIGATION_TEXT_MANUFACTURER),
            _obligation_mint(_OBLIGATION_TEXT_IMPORTER),
            _capability_mint(_CAPABILITY_NAME_MANUFACTURER),
            _capability_mint(_CAPABILITY_NAME_IMPORTER),
        ]
    )


def test_derivation_submits_obligations_capabilities_and_edges_with_the_role_and_requirement_endpoints_resolved_from_the_graph(  # noqa: E501 -- name fixed by the plan
    make_emitter: MakeEmitter, baseline_rig: BaselineRig
) -> None:
    """One group under the audit event; endpoints found in the graph.

    Order is Obligations, Capabilities, `HAS`, `SATISFIED_BY`, `REQUIRES` (the write order of the
    direct path); edge identities are `{type}:{source}->{target}`; the three edge types carry no
    properties; the Role and Requirement endpoints are outside the group, so the baseline handle
    receives the requirements read and one existing-node read per endpoint label.
    """
    emitter, _log_path = make_emitter()
    _seed_two_roles(baseline_rig)
    obligation_manufacturer = obligation_id(_ROLE_MANUFACTURER, _OBLIGATION_TEXT_MANUFACTURER)
    obligation_importer = obligation_id(_ROLE_IMPORTER, _OBLIGATION_TEXT_IMPORTER)
    capability_manufacturer = capability_id(_CAPABILITY_NAME_MANUFACTURER)
    capability_importer = capability_id(_CAPABILITY_NAME_IMPORTER)

    result = derive_obligations_and_capabilities(
        _INSTRUMENT_ID,
        **baseline_rig.stage_kwargs(),
        model="fake-model",
        call_completion=_two_role_script(),
        emitter=emitter,
    )

    [_seed_group, group] = baseline_rig.groups
    assert group.graph == "cra_baseline"
    assert group.audit_event_id == baseline_rig.audit_event_id
    assert [(entry.name, entry.identity) for entry in group.entries] == [
        ("Obligation", obligation_manufacturer),
        ("Obligation", obligation_importer),
        ("Capability", capability_manufacturer),
        ("Capability", capability_importer),
        ("HAS", f"HAS:{_ROLE_MANUFACTURER}->{obligation_manufacturer}"),
        ("HAS", f"HAS:{_ROLE_IMPORTER}->{obligation_importer}"),
        ("SATISFIED_BY", f"SATISFIED_BY:{_REQ_MANUFACTURER}->{obligation_manufacturer}"),
        ("SATISFIED_BY", f"SATISFIED_BY:{_REQ_IMPORTER}->{obligation_importer}"),
        ("REQUIRES", f"REQUIRES:{obligation_manufacturer}->{capability_manufacturer}"),
        ("REQUIRES", f"REQUIRES:{obligation_importer}->{capability_importer}"),
    ]
    for edge in (e for e in group.entries if e.name in {"HAS", "SATISFIED_BY", "REQUIRES"}):
        assert entry_properties(edge) == {}
    assert result.obligation_node_ids == (obligation_manufacturer, obligation_importer)
    assert result.capability_node_ids == (capability_manufacturer, capability_importer)
    assert ("Obligation", obligation_importer) in baseline_rig.baseline.nodes
    assert len(baseline_rig.entries_named("HAS")) == 2
    assert [q for q in baseline_rig.read_graph.queries if "OPTIONAL MATCH" not in q] == [
        "MATCH (n:Role) WHERE n.id IN $ids RETURN n.id",
        "MATCH (n:Requirement) WHERE n.id IN $ids RETURN n.id",
    ]


def _scripted_with_hook(responses: list[str], on_call: Callable[[], None]) -> CompletionCaller:
    """Like `_scripted`, and runs `on_call` at the start of every call (a concurrent writer)."""
    inner = _scripted(responses)

    def _call(*, model: str, messages: list[dict[str, str]], timeout: float) -> ModelResponse:
        on_call()
        return inner(model=model, messages=messages, timeout=timeout)

    return _call


def test_derivation_drops_an_edge_whose_role_or_requirement_is_missing_everywhere_without_error_or_log_entry(  # noqa: E501 -- name fixed by the plan
    make_emitter: MakeEmitter, read_lines: ReadLines, baseline_rig: BaselineRig
) -> None:
    """An edge whose Role or Requirement exists nowhere is skipped, as `MATCH` did.

    The Importer Role and its Requirement disappear (a concurrent writer) after the stage has
    read them and before it writes: the stage neither raises nor reports the dropped edges, and
    every other node and edge is present.
    """
    emitter, log_path = make_emitter()
    _seed_two_roles(baseline_rig)

    def remove_the_importer() -> None:
        baseline_rig.baseline.nodes.pop(("Role", _ROLE_IMPORTER), None)
        baseline_rig.baseline.nodes.pop(("Requirement", _REQ_IMPORTER), None)

    obligation_manufacturer = obligation_id(_ROLE_MANUFACTURER, _OBLIGATION_TEXT_MANUFACTURER)
    obligation_importer = obligation_id(_ROLE_IMPORTER, _OBLIGATION_TEXT_IMPORTER)
    call_completion = _scripted_with_hook(
        [
            _obligation_mint(_OBLIGATION_TEXT_MANUFACTURER),
            _obligation_mint(_OBLIGATION_TEXT_IMPORTER),
            _capability_mint(_CAPABILITY_NAME_MANUFACTURER),
            _capability_mint(_CAPABILITY_NAME_IMPORTER),
        ],
        remove_the_importer,
    )

    result = derive_obligations_and_capabilities(
        _INSTRUMENT_ID,
        **baseline_rig.stage_kwargs(),
        model="fake-model",
        call_completion=call_completion,
        emitter=emitter,
    )

    [_seed_group, group] = baseline_rig.groups
    identities = [entry.identity for entry in group.entries]
    assert f"HAS:{_ROLE_MANUFACTURER}->{obligation_manufacturer}" in identities
    assert f"SATISFIED_BY:{_REQ_MANUFACTURER}->{obligation_manufacturer}" in identities
    assert f"HAS:{_ROLE_IMPORTER}->{obligation_importer}" not in identities
    assert f"SATISFIED_BY:{_REQ_IMPORTER}->{obligation_importer}" not in identities
    assert obligation_importer in identities  # the node itself is still written
    assert len(group.entries) == 8
    assert result.obligation_node_ids == (obligation_manufacturer, obligation_importer)
    logged_text = str(read_lines(log_path))
    assert _ROLE_IMPORTER not in logged_text
    assert _REQ_IMPORTER not in logged_text


def test_derivation_with_nothing_to_write_submits_no_group(
    make_emitter: MakeEmitter, baseline_rig: BaselineRig
) -> None:
    """Empty collections: no stamp, no read of the endpoints, no group (`min_length=1`).

    A legacy, identity-less edge on the graph proves the stamp did not run.
    """
    emitter, _log_path = make_emitter()
    baseline_rig.baseline.seed_legacy_edge(
        ("DEFINES", "RegulatoryInstrument", _INSTRUMENT_ID, "Role", _ROLE_MANUFACTURER), {}
    )

    result = derive_obligations_and_capabilities(
        _INSTRUMENT_ID,
        **baseline_rig.stage_kwargs(),
        model="fake-model",
        call_completion=_scripted([]),
        emitter=emitter,
    )

    assert result.obligation_node_ids == ()
    assert result.capability_node_ids == ()
    assert baseline_rig.groups == []
    assert baseline_rig.logged() == []
    assert [q for q in baseline_rig.read_graph.queries if "OPTIONAL MATCH" not in q] == []
    assert [key[5] for key in baseline_rig.baseline.edges] == [""]  # still identity-less


def test_derivation_stamps_legacy_edges_before_its_group(
    make_emitter: MakeEmitter, baseline_rig: BaselineRig
) -> None:
    """A stage run alone on resume stamps the graph's legacy edges first, under the same event."""
    emitter, _log_path = make_emitter()
    _seed_two_roles(baseline_rig)
    baseline_rig.baseline.seed_legacy_edge(
        ("DEFINES", "RegulatoryInstrument", _INSTRUMENT_ID, "Role", _ROLE_MANUFACTURER), {}
    )

    derive_obligations_and_capabilities(
        _INSTRUMENT_ID,
        **baseline_rig.stage_kwargs(),
        model="fake-model",
        call_completion=_two_role_script(),
        emitter=emitter,
    )

    [_seed_group, stamp_group, group] = baseline_rig.groups
    assert stamp_group.audit_event_id == group.audit_event_id == baseline_rig.audit_event_id
    assert [entry.name for entry in stamp_group.entries] == ["DEFINES"]
    defines = [key for key in baseline_rig.baseline.edges if key[0] == "DEFINES"]
    assert [key[5] for key in defines] == [f"DEFINES:{_INSTRUMENT_ID}->{_ROLE_MANUFACTURER}"]


_OBLIGATION = "obligation_x"
_CAPABILITY = "capability_x"


def _write(rig: BaselineRig, **overrides: object) -> None:
    """Call the writer directly with one Obligation, one Capability and their edges."""
    collections: dict[str, object] = {
        "obligation_nodes": (ObligationNode(_OBLIGATION, {"text": "Do it."}),),
        "capability_nodes": (CapabilityNode(_CAPABILITY, {"name": "Doing"}),),
        "has_edges": (ObligationHasEdge(_ROLE_MANUFACTURER, _OBLIGATION),),
        "satisfied_by_edges": (RequirementSatisfiedByEdge(_REQ_MANUFACTURER, _OBLIGATION),),
        "requires_edges": (CapabilityRequiresEdge(_OBLIGATION, _CAPABILITY),),
        **overrides,
    }
    persist_obligation_and_capability_graph(
        rig.submitter,
        rig.baseline_graph_name,
        rig.audit_event_id,
        graph=rig.read_graph,
        **collections,  # pyright: ignore[reportArgumentType] -- the five collections, typed above
    )


def test_a_node_and_edges_repeated_in_one_derivation_collapse_like_the_old_merge(
    baseline_rig: BaselineRig, other_baseline_rig: BaselineRig
) -> None:
    """F5: the same Obligation id, Capability id and edges twice make one primitive each.

    Later properties override earlier ones and keep earlier-only keys (sequential
    `SET n += props`); the graph's digest equals the one built from the already-folded input.
    """
    for rig in (baseline_rig, other_baseline_rig):
        seed_extraction_output(
            rig,
            _INSTRUMENT_ID,
            {_ROLE_MANUFACTURER: "Manufacturer"},
            [(_REQ_MANUFACTURER, "Conduct it.", _ROLE_MANUFACTURER)],
        )

    _write(
        baseline_rig,
        obligation_nodes=(
            ObligationNode(_OBLIGATION, {"text": "Do it.", "confidence": 0.5}),
            ObligationNode(_OBLIGATION, {"confidence": 0.9}),
        ),
        capability_nodes=(
            CapabilityNode(_CAPABILITY, {"name": "Doing"}),
            CapabilityNode(_CAPABILITY, {"name": "Doing", "description": "Does."}),
        ),
        has_edges=(ObligationHasEdge(_ROLE_MANUFACTURER, _OBLIGATION),) * 2,
        satisfied_by_edges=(RequirementSatisfiedByEdge(_REQ_MANUFACTURER, _OBLIGATION),) * 2,
        requires_edges=(CapabilityRequiresEdge(_OBLIGATION, _CAPABILITY),) * 2,
    )
    _write(
        other_baseline_rig,
        obligation_nodes=(ObligationNode(_OBLIGATION, {"text": "Do it.", "confidence": 0.9}),),
        capability_nodes=(CapabilityNode(_CAPABILITY, {"name": "Doing", "description": "Does."}),),
    )

    [_seed_group, group] = baseline_rig.groups
    assert [(entry.name, entry.identity) for entry in group.entries] == [
        ("Obligation", _OBLIGATION),
        ("Capability", _CAPABILITY),
        ("HAS", f"HAS:{_ROLE_MANUFACTURER}->{_OBLIGATION}"),
        ("SATISFIED_BY", f"SATISFIED_BY:{_REQ_MANUFACTURER}->{_OBLIGATION}"),
        ("REQUIRES", f"REQUIRES:{_OBLIGATION}->{_CAPABILITY}"),
    ]
    assert entry_properties(group.entries[0]) == {"text": "Do it.", "confidence": 0.9}
    assert canonical_digest(baseline_rig.baseline) == canonical_digest(other_baseline_rig.baseline)


def test_a_group_pending_from_an_earlier_run_raises_the_persistence_error_and_writes_nothing(
    baseline_rig: BaselineRig,
) -> None:
    """F6: with an earlier group committed but not applied, the writer must not read or write.

    Reading first could find the Role missing only because the earlier group is not applied, and
    drop its edge silently. It fails with the persistence error before the read.
    """
    baseline_rig.baseline.fail_on_write(redis.exceptions.ConnectionError("Error 111 connecting"))
    seed_extraction_output(
        baseline_rig,
        _INSTRUMENT_ID,
        {_ROLE_MANUFACTURER: "Manufacturer"},
        [(_REQ_MANUFACTURER, "Conduct it.", _ROLE_MANUFACTURER)],
    )

    with pytest.raises(DomainMapperPersistenceError, match="FalkorDB write failed: "):
        _write(baseline_rig)

    assert len(baseline_rig.groups) == 1  # only the pending earlier group
    assert baseline_rig.read_graph.queries == []


def test_an_unstorable_derived_property_raises_the_persistence_error_with_an_empty_log(
    baseline_rig: BaselineRig,
) -> None:
    """A property the graph cannot store fails before anything is stamped, read or submitted."""
    with pytest.raises(DomainMapperPersistenceError, match="has a property the graph cannot store"):
        _write(
            baseline_rig,
            obligation_nodes=(ObligationNode(_OBLIGATION, {"confidence": float("inf")}),),
        )

    assert baseline_rig.groups == []
    assert baseline_rig.logged() == []
    assert baseline_rig.read_graph.queries == []


def test_persisting_derived_ids_calls_no_llm_and_keeps_every_derived_id(
    make_emitter: MakeEmitter, baseline_rig: BaselineRig
) -> None:
    """The writer mints and matches nothing; the ids written are the derived ones.

    The scripted LLM is exhausted by the four derivation calls and fails on a fifth, so a
    writer-side call would fail this test; the group's node ids equal the result's.
    """
    emitter, _log_path = make_emitter()
    _seed_two_roles(baseline_rig)

    result = derive_obligations_and_capabilities(
        _INSTRUMENT_ID,
        **baseline_rig.stage_kwargs(),
        model="fake-model",
        call_completion=_two_role_script(),
        emitter=emitter,
    )

    written = {entry.identity for entry in baseline_rig.entries_named("Obligation")}
    written |= {entry.identity for entry in baseline_rig.entries_named("Capability")}
    assert written == {*result.obligation_node_ids, *result.capability_node_ids}


def test_a_group_pending_from_an_earlier_run_stops_the_stage_before_its_first_read_and_llm_call(
    make_emitter: MakeEmitter, baseline_rig: BaselineRig
) -> None:
    """4e: a solo resume must not derive from a stale baseline.

    Extraction's group is committed but not applied (FalkorDB write fault), so the Requirements
    read could see an incomplete graph. The stage fails with the persistence error before that
    read, before any LLM call and without writing.
    """
    emitter, _log_path = make_emitter()
    baseline_rig.baseline.fail_on_write(redis.exceptions.ConnectionError("Error 111 connecting"))
    _seed_two_roles(baseline_rig)
    llm_calls: list[str] = []

    def _no_llm(*, model: str, messages: list[dict[str, str]], timeout: float) -> ModelResponse:
        llm_calls.append(model)
        return _response(_obligation_mint(_OBLIGATION_TEXT_MANUFACTURER))

    with pytest.raises(DomainMapperPersistenceError, match="FalkorDB write failed: "):
        derive_obligations_and_capabilities(
            _INSTRUMENT_ID,
            **baseline_rig.stage_kwargs(),
            model="fake-model",
            call_completion=_no_llm,
            emitter=emitter,
        )

    assert llm_calls == []
    assert len(baseline_rig.groups) == 1  # only the pending earlier group
    assert baseline_rig.read_graph.queries == []
