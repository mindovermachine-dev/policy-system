"""Tests for ps_service.domain_mapper.prompts."""

from __future__ import annotations

import json

import pytest

from ps_service.domain_mapper.errors import (
    DomainMapperDerivationError,
    DomainMapperExtractionError,
)
from ps_service.domain_mapper.identity import capability_id, obligation_id
from ps_service.domain_mapper.models import (
    CapabilityDecision,
    CapabilityReuseVerdict,
    DefinedTermCandidate,
    ExtractionUnit,
    ObligationAssignment,
    RequirementCandidate,
)
from ps_service.domain_mapper.prompts import (
    CAPABILITY_DERIVATION_SYSTEM_PROMPT,
    CAPABILITY_REUSE_VERIFICATION_SYSTEM_PROMPT,
    EXTRACTION_SYSTEM_PROMPT,
    parse_capability_response,
    parse_capability_reuse_verdict,
    parse_definitions_response,
    parse_extraction_response,
    parse_obligation_response,
)

_UNIT = ExtractionUnit(
    citation_ref="Art. 13(1)",
    text="The manufacturer shall conduct a cybersecurity risk assessment.",
    article_number="13",
    paragraph_number="1",
    article_heading="Obligations of manufacturers",
)


def _valid_payload() -> dict[str, object]:
    return {
        "requirements": [
            {
                "role_name": "Manufacturer",
                "role_is_actor": True,
                "text": "Conduct a cybersecurity risk assessment.",
                "type": "requirement",
                "letter_suffix": None,
                "confidence": 0.92,
            }
        ]
    }


def test_parse_extraction_response_valid_json_returns_populated_candidates() -> None:
    text = json.dumps(_valid_payload())

    candidates = parse_extraction_response(text, _UNIT)

    assert len(candidates) == 1
    candidate = candidates[0]
    assert isinstance(candidate, RequirementCandidate)
    # Fields sourced from the unit.
    assert candidate.unit_citation_ref == "Art. 13(1)"
    assert candidate.unit_article_number == "13"
    assert candidate.unit_paragraph_number == "1"
    # Fields sourced from the LLM's JSON.
    assert candidate.role_name == "Manufacturer"
    assert candidate.text == "Conduct a cybersecurity risk assessment."
    assert candidate.type == "requirement"
    assert candidate.letter_suffix is None
    assert candidate.confidence == 0.92


def test_parse_extraction_response_multiple_candidates_all_populated() -> None:
    payload = {
        "requirements": [
            {
                "role_name": "Manufacturer",
                "role_is_actor": True,
                "text": "Conduct a risk assessment.",
                "type": "requirement",
                "letter_suffix": "a",
                "confidence": 0.9,
            },
            {
                "role_name": "Manufacturer",
                "role_is_actor": True,
                "text": "Not place unsafe products on the market.",
                "type": "prohibition",
                "letter_suffix": "b",
                "confidence": 0.8,
            },
        ]
    }

    candidates = parse_extraction_response(json.dumps(payload), _UNIT)

    assert [c.letter_suffix for c in candidates] == ["a", "b"]
    assert [c.type for c in candidates] == ["requirement", "prohibition"]
    assert all(c.unit_citation_ref == "Art. 13(1)" for c in candidates)


def test_parse_extraction_response_empty_requirements_returns_empty_list() -> None:
    candidates = parse_extraction_response(json.dumps({"requirements": []}), _UNIT)
    assert candidates == []


def test_parse_extraction_response_malformed_json_raises_typed_error() -> None:
    with pytest.raises(DomainMapperExtractionError) as exc_info:
        parse_extraction_response("{not valid json", _UNIT)
    assert "Art. 13(1)" in str(exc_info.value)
    assert exc_info.value.error_kind == "invalid_json"


def test_parse_extraction_response_missing_requirements_key_raises_typed_error() -> None:
    with pytest.raises(DomainMapperExtractionError) as exc_info:
        parse_extraction_response(json.dumps({"unexpected": []}), _UNIT)
    assert "Art. 13(1)" in str(exc_info.value)
    assert exc_info.value.error_kind == "missing_requirements_key"


def test_parse_extraction_response_item_missing_confidence_raises_typed_error() -> None:
    payload = {
        "requirements": [
            {
                "role_name": "Manufacturer",
                "role_is_actor": True,
                "text": "Conduct a cybersecurity risk assessment.",
                "type": "requirement",
                "letter_suffix": None,
                # confidence deliberately omitted
            }
        ]
    }

    with pytest.raises(DomainMapperExtractionError) as exc_info:
        parse_extraction_response(json.dumps(payload), _UNIT)
    assert "Art. 13(1)" in str(exc_info.value)
    assert exc_info.value.error_kind == "invalid_requirement_item"


def test_extraction_prompt_return_shape_asks_for_the_actor_flag() -> None:
    assert '"role_is_actor": bool' in EXTRACTION_SYSTEM_PROMPT


def test_extraction_prompt_defines_role_name_as_one_named_actor() -> None:
    assert "ONE named duty-bearing actor" in EXTRACTION_SYSTEM_PROMPT
    assert "never a list or an alternative of actors" in EXTRACTION_SYSTEM_PROMPT
    assert '"importer or distributor"' in EXTRACTION_SYSTEM_PROMPT


def test_extraction_prompt_splits_a_duty_on_several_actors_into_one_requirement_each() -> None:
    assert "one requirement per independently-required duty AND per actor" in (
        EXTRACTION_SYSTEM_PROMPT
    )
    assert "return one requirement per actor" in EXTRACTION_SYSTEM_PROMPT
    assert 'letter_suffix "a", "b", "c"' in EXTRACTION_SYSTEM_PROMPT


@pytest.mark.parametrize(
    "non_actor_example",
    [
        "the technical documentation",
        "a fine or fee",
        "an event",
        "a product",
        '"that body"',
        '"the person referred to in paragraph 1"',
        '"the offender"',
    ],
)
def test_extraction_prompt_lists_what_is_not_an_actor(non_actor_example: str) -> None:
    flattened = " ".join(EXTRACTION_SYSTEM_PROMPT.split())

    assert "Set it to false when role_name is not such an actor" in flattened
    assert non_actor_example in flattened


def _item(**overrides: object) -> dict[str, object]:
    fields: dict[str, object] = {
        "role_name": "Manufacturer",
        "role_is_actor": True,
        "text": "Conduct a cybersecurity risk assessment.",
        "type": "requirement",
        "letter_suffix": None,
        "confidence": 0.9,
    }
    fields.update(overrides)
    return fields


@pytest.mark.parametrize("flag", [True, False])
def test_parse_extraction_response_carries_the_actor_flag_as_given(*, flag: bool) -> None:
    payload = {"requirements": [_item(role_is_actor=flag)]}

    (candidate,) = parse_extraction_response(json.dumps(payload), _UNIT)

    assert candidate.role_is_actor is flag


def test_parse_extraction_response_item_missing_the_actor_flag_raises_typed_error() -> None:
    item = _item()
    del item["role_is_actor"]

    with pytest.raises(DomainMapperExtractionError) as exc_info:
        parse_extraction_response(json.dumps({"requirements": [item]}), _UNIT)

    assert exc_info.value.error_kind == "invalid_requirement_item"
    assert "role_is_actor" in str(exc_info.value)


@pytest.mark.parametrize("flag", ["true", "yes", 1, 0, None, "false"])
def test_parse_extraction_response_item_with_a_non_boolean_actor_flag_raises_typed_error(
    flag: object,
) -> None:
    payload = {"requirements": [_item(role_is_actor=flag)]}

    with pytest.raises(DomainMapperExtractionError) as exc_info:
        parse_extraction_response(json.dumps(payload), _UNIT)

    assert exc_info.value.error_kind == "invalid_requirement_item"


def test_parse_extraction_response_item_invalid_type_raises_typed_error() -> None:
    payload = {
        "requirements": [
            {
                "role_name": "Manufacturer",
                "role_is_actor": True,
                "text": "Conduct a cybersecurity risk assessment.",
                "type": "not-a-real-type",
                "letter_suffix": None,
                "confidence": 0.5,
            }
        ]
    }

    with pytest.raises(DomainMapperExtractionError) as exc_info:
        parse_extraction_response(json.dumps(payload), _UNIT)
    assert exc_info.value.error_kind == "invalid_requirement_item"


def test_parse_extraction_response_requirements_not_a_list_raises_typed_error() -> None:
    with pytest.raises(DomainMapperExtractionError) as exc_info:
        parse_extraction_response(json.dumps({"requirements": "not-a-list"}), _UNIT)
    assert "Art. 13(1)" in str(exc_info.value)
    assert exc_info.value.error_kind == "non_list_requirements"


def test_parse_extraction_response_non_object_item_raises_typed_error() -> None:
    payload = {"requirements": ["not-an-object"]}

    with pytest.raises(DomainMapperExtractionError) as exc_info:
        parse_extraction_response(json.dumps(payload), _UNIT)
    assert "Art. 13(1)" in str(exc_info.value)
    assert exc_info.value.error_kind == "non_object_item"


# --- parse_definitions_response ---------------------------------------------

_DEFINITIONS_UNIT = ExtractionUnit(
    citation_ref="Art. 2(1)",
    text="'Manufacturer' means any natural or legal person who develops or manufactures products.",
    article_number="2",
    paragraph_number="1",
    article_heading="Definitions",
)


def test_parse_definitions_response_valid_json_returns_populated_candidates() -> None:
    payload = {"terms": ["Manufacturer", "Importer"]}

    candidates = parse_definitions_response(json.dumps(payload), _DEFINITIONS_UNIT)

    assert len(candidates) == 2
    assert all(isinstance(c, DefinedTermCandidate) for c in candidates)
    assert [c.term for c in candidates] == ["Manufacturer", "Importer"]
    assert all(c.citation_ref == "Art. 2(1)" for c in candidates)


def test_parse_definitions_response_empty_terms_returns_empty_list() -> None:
    candidates = parse_definitions_response(json.dumps({"terms": []}), _DEFINITIONS_UNIT)
    assert candidates == []


def test_parse_definitions_response_malformed_json_raises_typed_error() -> None:
    with pytest.raises(DomainMapperExtractionError) as exc_info:
        parse_definitions_response("{not valid json", _DEFINITIONS_UNIT)
    assert "Art. 2(1)" in str(exc_info.value)
    assert exc_info.value.error_kind == "invalid_definitions_json"


def test_parse_definitions_response_missing_terms_key_raises_typed_error() -> None:
    with pytest.raises(DomainMapperExtractionError) as exc_info:
        parse_definitions_response(json.dumps({"unexpected": []}), _DEFINITIONS_UNIT)
    assert "Art. 2(1)" in str(exc_info.value)
    assert exc_info.value.error_kind == "missing_terms_key"


def test_parse_definitions_response_non_list_terms_raises_typed_error() -> None:
    with pytest.raises(DomainMapperExtractionError) as exc_info:
        parse_definitions_response(json.dumps({"terms": "Manufacturer"}), _DEFINITIONS_UNIT)
    assert "Art. 2(1)" in str(exc_info.value)
    assert exc_info.value.error_kind == "non_list_terms"


def test_parse_definitions_response_non_string_term_item_raises_typed_error() -> None:
    payload = {"terms": [{"name": "Manufacturer"}]}

    with pytest.raises(DomainMapperExtractionError) as exc_info:
        parse_definitions_response(json.dumps(payload), _DEFINITIONS_UNIT)
    assert "Art. 2(1)" in str(exc_info.value)
    assert exc_info.value.error_kind == "invalid_defined_term_item"


def test_parse_definitions_response_empty_string_term_item_raises_typed_error() -> None:
    with pytest.raises(DomainMapperExtractionError) as exc_info:
        parse_definitions_response(json.dumps({"terms": [""]}), _DEFINITIONS_UNIT)
    assert "Art. 2(1)" in str(exc_info.value)
    assert exc_info.value.error_kind == "invalid_defined_term_item"


# --- parse_obligation_response ----------------------------------------------

_REQUIREMENT_ID = "CRA_req_art_13.1"
_ROLE_NODE_ID = "role_manufacturer_abc123"
_EXISTING_TEXT = "Conduct Cybersecurity Risk Assessment"
_EXISTING_ID = obligation_id(_ROLE_NODE_ID, _EXISTING_TEXT)
_ROLE_VIEW = {_EXISTING_ID: _EXISTING_TEXT}


def _match_payload(matched_existing_id: str, confidence: float = 0.9) -> str:
    return json.dumps(
        {
            "matched_existing_id": matched_existing_id,
            "new_text": None,
            "unmatchable": False,
            "confidence": confidence,
        }
    )


def _mint_payload(new_text: str, confidence: float = 0.9) -> str:
    return json.dumps(
        {
            "matched_existing_id": None,
            "new_text": new_text,
            "unmatchable": False,
            "confidence": confidence,
        }
    )


def _unmatchable_payload(confidence: float = 0.5) -> str:
    return json.dumps(
        {
            "matched_existing_id": None,
            "new_text": None,
            "unmatchable": True,
            "confidence": confidence,
        }
    )


def test_parse_obligation_response_match_resolves_registry_text_and_id() -> None:
    assignment = parse_obligation_response(
        _match_payload(_EXISTING_ID), _REQUIREMENT_ID, _ROLE_NODE_ID, _ROLE_VIEW
    )

    assert isinstance(assignment, ObligationAssignment)
    assert assignment.requirement_id == _REQUIREMENT_ID
    assert assignment.role_node_id == _ROLE_NODE_ID
    assert assignment.obligation_node_id == _EXISTING_ID
    assert assignment.obligation_text == _EXISTING_TEXT
    assert assignment.confidence == 0.9


def test_parse_obligation_response_mint_derives_id_from_new_text() -> None:
    new_text = "Report Security Incidents"

    assignment = parse_obligation_response(
        _mint_payload(new_text, confidence=0.75), _REQUIREMENT_ID, _ROLE_NODE_ID, _ROLE_VIEW
    )

    assert assignment.obligation_node_id == obligation_id(_ROLE_NODE_ID, new_text)
    assert assignment.obligation_text == new_text
    assert assignment.confidence == 0.75


def test_parse_obligation_response_unmatchable_has_no_obligation() -> None:
    assignment = parse_obligation_response(
        _unmatchable_payload(confidence=0.3), _REQUIREMENT_ID, _ROLE_NODE_ID, _ROLE_VIEW
    )

    assert assignment.obligation_node_id is None
    assert assignment.obligation_text is None
    assert assignment.confidence == 0.3


def test_parse_obligation_response_malformed_json_raises_typed_error() -> None:
    with pytest.raises(DomainMapperDerivationError) as exc_info:
        parse_obligation_response("{not valid json", _REQUIREMENT_ID, _ROLE_NODE_ID, _ROLE_VIEW)
    assert _REQUIREMENT_ID in str(exc_info.value)


def test_parse_obligation_response_neither_matched_new_nor_unmatchable_raises_typed_error() -> None:
    """The exact LEARNINGS.md B1-documented failure shape: a syntactically
    valid response with neither a valid match nor a new-text value nor
    unmatchable=true set.
    """
    payload = json.dumps(
        {"matched_existing_id": None, "new_text": None, "unmatchable": False, "confidence": 0.5}
    )

    with pytest.raises(DomainMapperDerivationError) as exc_info:
        parse_obligation_response(payload, _REQUIREMENT_ID, _ROLE_NODE_ID, _ROLE_VIEW)
    assert _REQUIREMENT_ID in str(exc_info.value)


def test_parse_obligation_response_matched_id_not_in_role_view_raises_typed_error() -> None:
    """A matched_existing_id that doesn't resolve within this Role's own
    registry view (a hallucinated match) is treated the same as a missing
    match — a typed error, not a silently-accepted dangling reference.
    """
    payload = _match_payload("obl_nonexistent_000000")

    with pytest.raises(DomainMapperDerivationError) as exc_info:
        parse_obligation_response(payload, _REQUIREMENT_ID, _ROLE_NODE_ID, _ROLE_VIEW)
    assert _REQUIREMENT_ID in str(exc_info.value)


def test_parse_obligation_response_missing_confidence_raises_typed_error() -> None:
    payload = json.dumps(
        {"matched_existing_id": None, "new_text": "Report Security Incidents", "unmatchable": False}
    )

    with pytest.raises(DomainMapperDerivationError) as exc_info:
        parse_obligation_response(payload, _REQUIREMENT_ID, _ROLE_NODE_ID, _ROLE_VIEW)
    assert _REQUIREMENT_ID in str(exc_info.value)


# --- parse_capability_response ----------------------------------------------

_OBLIGATION_NODE_ID = "obl_conduct_risk_assessment_abc123"
_EXISTING_CAPABILITY_NAME = "Data Encryption"
_EXISTING_CAPABILITY_ID = capability_id(_EXISTING_CAPABILITY_NAME)
_EXISTING_CAPABILITY_DESCRIPTION = "Encrypts data at rest and in transit."
_CAPABILITY_REGISTRY: dict[str, tuple[str, str | None]] = {
    _EXISTING_CAPABILITY_ID: (_EXISTING_CAPABILITY_NAME, _EXISTING_CAPABILITY_DESCRIPTION)
}


def _capability_payload(*items: dict[str, object]) -> str:
    return json.dumps({"capabilities": list(items)})


def _match_item(matched_existing_id: str, confidence: float = 0.9) -> dict[str, object]:
    return {
        "matched_existing_id": matched_existing_id,
        "new_name": None,
        "new_description": None,
        "confidence": confidence,
    }


def _mint_item(
    new_name: str, new_description: str | None = None, confidence: float = 0.9
) -> dict[str, object]:
    return {
        "matched_existing_id": None,
        "new_name": new_name,
        "new_description": new_description,
        "confidence": confidence,
    }


def test_parse_capability_response_single_match_resolves_registry_entry() -> None:
    payload = _capability_payload(_match_item(_EXISTING_CAPABILITY_ID))

    decisions = parse_capability_response(payload, _OBLIGATION_NODE_ID, _CAPABILITY_REGISTRY)

    assert len(decisions) == 1
    decision = decisions[0]
    assert isinstance(decision, CapabilityDecision)
    assert decision.obligation_node_id == _OBLIGATION_NODE_ID
    assert decision.capability_node_id == _EXISTING_CAPABILITY_ID
    assert decision.name == _EXISTING_CAPABILITY_NAME
    assert decision.description == _EXISTING_CAPABILITY_DESCRIPTION
    assert decision.confidence == 0.9


def test_parse_capability_response_single_mint_derives_id_from_new_name() -> None:
    payload = _capability_payload(
        _mint_item("Security Logging", "Logs security-relevant events.", confidence=0.7)
    )

    decisions = parse_capability_response(payload, _OBLIGATION_NODE_ID, {})

    assert len(decisions) == 1
    decision = decisions[0]
    assert decision.capability_node_id == capability_id("Security Logging")
    assert decision.name == "Security Logging"
    assert decision.description == "Logs security-relevant events."
    assert decision.confidence == 0.7
    assert decision.is_reuse is False


def test_parse_capability_response_multi_capability_response_returns_two_decisions() -> None:
    """Proves the list-of-decisions shape actually supports >1 -- a single
    Obligation may bundle more than one distinct Capability requirement
    (multi-capability-per-Obligation).
    """
    payload = _capability_payload(
        _mint_item("Incident Detection", "Detects security incidents in real time."),
        _mint_item("Regulatory Notification Workflow", "Notifies the authority in time."),
    )

    decisions = parse_capability_response(payload, _OBLIGATION_NODE_ID, {})

    assert len(decisions) == 2
    assert {d.name for d in decisions} == {
        "Incident Detection",
        "Regulatory Notification Workflow",
    }
    assert all(d.obligation_node_id == _OBLIGATION_NODE_ID for d in decisions)
    assert {d.capability_node_id for d in decisions} == {
        capability_id("Incident Detection"),
        capability_id("Regulatory Notification Workflow"),
    }


def test_parse_capability_response_malformed_json_raises_typed_error() -> None:
    with pytest.raises(DomainMapperDerivationError) as exc_info:
        parse_capability_response("{not valid json", _OBLIGATION_NODE_ID, {})
    assert _OBLIGATION_NODE_ID in str(exc_info.value)


def test_parse_capability_response_missing_capabilities_key_raises_typed_error() -> None:
    with pytest.raises(DomainMapperDerivationError) as exc_info:
        parse_capability_response(json.dumps({"unexpected": []}), _OBLIGATION_NODE_ID, {})
    assert _OBLIGATION_NODE_ID in str(exc_info.value)


def test_parse_capability_response_item_missing_both_matched_and_new_raises_typed_error() -> None:
    """The exact LEARNINGS.md B1-documented failure shape ported to
    Capability derivation: a syntactically valid item with neither a valid
    matched_existing_id nor a new_name value.
    """
    payload = _capability_payload(
        {"matched_existing_id": None, "new_name": None, "new_description": None, "confidence": 0.5}
    )

    with pytest.raises(DomainMapperDerivationError) as exc_info:
        parse_capability_response(payload, _OBLIGATION_NODE_ID, {})
    assert _OBLIGATION_NODE_ID in str(exc_info.value)


def test_parse_capability_response_matched_id_not_in_registry_raises_typed_error() -> None:
    """A matched_existing_id that doesn't resolve within the given registry
    (a hallucinated match) is treated as malformed, not silently accepted.
    """
    payload = _capability_payload(_match_item("cap_nonexistent_000000"))

    with pytest.raises(DomainMapperDerivationError) as exc_info:
        parse_capability_response(payload, _OBLIGATION_NODE_ID, {})
    assert _OBLIGATION_NODE_ID in str(exc_info.value)


def test_parse_capability_response_missing_confidence_raises_typed_error() -> None:
    payload = _capability_payload(
        {"matched_existing_id": None, "new_name": "Data Encryption", "new_description": None}
    )

    with pytest.raises(DomainMapperDerivationError) as exc_info:
        parse_capability_response(payload, _OBLIGATION_NODE_ID, {})
    assert _OBLIGATION_NODE_ID in str(exc_info.value)


def test_capability_derivation_prompt_requires_whole_duty_coverage_for_reuse() -> None:
    """Reuse only when the description covers the whole duty;
    otherwise mint a more specific Capability. JSON contract unchanged.
    """
    assert "whole duty" in CAPABILITY_DERIVATION_SYSTEM_PROMPT
    assert "more specific" in CAPABILITY_DERIVATION_SYSTEM_PROMPT
    for contract_key in ("matched_existing_id", "new_name", "new_description", "confidence"):
        assert contract_key in CAPABILITY_DERIVATION_SYSTEM_PROMPT


def test_parse_capability_response_match_item_is_marked_as_reuse() -> None:
    """A matched-existing item is a reuse proposal, so it is
    marked for verification.
    """
    payload = _capability_payload(_match_item(_EXISTING_CAPABILITY_ID))

    decisions = parse_capability_response(payload, _OBLIGATION_NODE_ID, _CAPABILITY_REGISTRY)

    assert decisions[0].is_reuse is True


def test_parse_capability_response_mint_of_registry_name_is_marked_as_reuse() -> None:
    """A "mint" whose capability_id is already registered attaches
    to the existing Capability, so it is a reuse carrying the registry's own
    name and description (the model's new_description is discarded).
    """
    payload = _capability_payload(_mint_item(_EXISTING_CAPABILITY_NAME, "Other text."))

    decisions = parse_capability_response(payload, _OBLIGATION_NODE_ID, _CAPABILITY_REGISTRY)

    decision = decisions[0]
    assert decision.is_reuse is True
    assert decision.capability_node_id == _EXISTING_CAPABILITY_ID
    assert decision.description == _EXISTING_CAPABILITY_DESCRIPTION


def test_capability_reuse_verification_prompt_states_whole_duty_rule_and_json_contract() -> None:
    assert "whole duty" in CAPABILITY_REUSE_VERIFICATION_SYSTEM_PROMPT
    for contract_key in ("verdict", "new_name", "new_description", "confidence"):
        assert contract_key in CAPABILITY_REUSE_VERIFICATION_SYSTEM_PROMPT


def test_capability_reuse_verification_prompt_lets_broader_description_cover_narrower_duty() -> (
    None
):
    """The verifier is told that a broader description can
    cover a narrower duty, judged by the description rather than the name,
    while partial overlap and over-general descriptions still do not count.
    """
    for phrase in (
        "A broader description can cover a narrower duty",
        "not by how its name is worded",
        "does not reach",
        "partial overlap is not enough",
        "whole duty",
        "too general to cover a specific duty",
        "Reuse is the default",
    ):
        assert phrase in CAPABILITY_REUSE_VERIFICATION_SYSTEM_PROMPT


def test_capability_reuse_verification_prompt_treats_domain_qualifier_alone_as_not_distinct() -> (
    None
):
    """The verifier is told that a domain qualifier such as
    "ICT" names the setting, not a different ability, in either direction,
    while capacities naming different domains are still distinct.
    """
    for phrase in (
        "A domain qualifier alone does not make two capacities distinct",
        "present on one side, absent on the other",
        "name different domains",
        "not a different ability",
        "not confined to",
    ):
        assert phrase in CAPABILITY_REUSE_VERIFICATION_SYSTEM_PROMPT


def test_parse_capability_reuse_verdict_accepts_ict_incident_reporting_under_eu_regulatory_reporting() -> (  # noqa: E501
    None
):
    """Characterisation pin. An
    "accept" for the DORA subsumption pair parses to an accept verdict
    under the real Obligation and Capability ids.
    """
    text = json.dumps(
        {"verdict": "accept", "new_name": None, "new_description": None, "confidence": 0.9}
    )

    verdict = parse_capability_reuse_verdict(
        text,
        "obl_report_major_ict_related_incidents_to_the_competent_authority_db689a",
        "cap_eu_regulatory_reporting_16d668",
    )

    assert verdict.verdict == "accept"
    assert verdict.new_name is None
    assert verdict.new_description is None


def test_parse_capability_reuse_verdict_accept_returns_accept_verdict() -> None:
    text = json.dumps(
        {"verdict": "accept", "new_name": None, "new_description": None, "confidence": 0.9}
    )

    verdict = parse_capability_reuse_verdict(text, _OBLIGATION_NODE_ID, _EXISTING_CAPABILITY_ID)

    assert isinstance(verdict, CapabilityReuseVerdict)
    assert verdict.verdict == "accept"
    assert verdict.new_name is None
    assert verdict.new_description is None


def test_parse_capability_reuse_verdict_accept_without_confidence_returns_accept_verdict() -> None:
    """A-m7: confidence is optional on accept."""
    text = json.dumps({"verdict": "accept"})

    verdict = parse_capability_reuse_verdict(text, _OBLIGATION_NODE_ID, _EXISTING_CAPABILITY_ID)

    assert verdict.verdict == "accept"
    assert verdict.confidence is None


@pytest.mark.parametrize(
    "response_text",
    [
        pytest.param("{not valid json", id="invalid_json"),
        pytest.param(json.dumps(["accept"]), id="non_object"),
        pytest.param(json.dumps({"new_name": "X", "confidence": 0.8}), id="missing_verdict"),
        pytest.param(json.dumps({"verdict": "maybe"}), id="unknown_verdict"),
    ],
)
def test_parse_capability_reuse_verdict_malformed_response_raises_naming_both_ids(
    response_text: str,
) -> None:
    with pytest.raises(DomainMapperDerivationError) as exc_info:
        parse_capability_reuse_verdict(response_text, _OBLIGATION_NODE_ID, _EXISTING_CAPABILITY_ID)
    assert _OBLIGATION_NODE_ID in str(exc_info.value)
    assert _EXISTING_CAPABILITY_ID in str(exc_info.value)


def test_parse_capability_reuse_verdict_reject_with_name_description_and_confidence_returns_reject_verdict() -> (  # noqa: E501
    None
):
    text = json.dumps(
        {
            "verdict": "reject",
            "new_name": "Organisational Continuity Structure",
            "new_description": "Maintains continuity structure.",
            "confidence": 0.8,
        }
    )

    verdict = parse_capability_reuse_verdict(text, _OBLIGATION_NODE_ID, _EXISTING_CAPABILITY_ID)

    assert verdict.verdict == "reject"
    assert verdict.new_name == "Organisational Continuity Structure"
    assert verdict.new_description == "Maintains continuity structure."
    assert verdict.confidence == 0.8


def _reject_payload(**overrides: object) -> str:
    payload: dict[str, object] = {
        "verdict": "reject",
        "new_name": "Organisational Continuity Structure",
        "new_description": "Maintains continuity structure.",
        "confidence": 0.8,
    }
    payload.update(overrides)
    return json.dumps({key: value for key, value in payload.items() if value is not ...})


@pytest.mark.parametrize(
    "new_name", [pytest.param(None, id="null"), pytest.param("  ", id="empty")]
)
def test_parse_capability_reuse_verdict_reject_without_new_name_raises_naming_both_ids(
    new_name: str | None,
) -> None:
    with pytest.raises(DomainMapperDerivationError) as exc_info:
        parse_capability_reuse_verdict(
            _reject_payload(new_name=new_name), _OBLIGATION_NODE_ID, _EXISTING_CAPABILITY_ID
        )
    assert _OBLIGATION_NODE_ID in str(exc_info.value)
    assert _EXISTING_CAPABILITY_ID in str(exc_info.value)


@pytest.mark.parametrize(
    "new_description", [pytest.param(None, id="null"), pytest.param("", id="empty")]
)
def test_parse_capability_reuse_verdict_reject_without_new_description_raises_naming_both_ids(
    new_description: str | None,
) -> None:
    with pytest.raises(DomainMapperDerivationError) as exc_info:
        parse_capability_reuse_verdict(
            _reject_payload(new_description=new_description),
            _OBLIGATION_NODE_ID,
            _EXISTING_CAPABILITY_ID,
        )
    assert _OBLIGATION_NODE_ID in str(exc_info.value)
    assert _EXISTING_CAPABILITY_ID in str(exc_info.value)


def test_parse_capability_reuse_verdict_reject_without_confidence_raises_naming_both_ids() -> None:
    """A-m7: confidence is required on reject."""
    with pytest.raises(DomainMapperDerivationError) as exc_info:
        parse_capability_reuse_verdict(
            _reject_payload(confidence=...), _OBLIGATION_NODE_ID, _EXISTING_CAPABILITY_ID
        )
    assert _OBLIGATION_NODE_ID in str(exc_info.value)
    assert _EXISTING_CAPABILITY_ID in str(exc_info.value)


def test_parse_capability_reuse_verdict_reject_with_out_of_range_confidence_raises_naming_both_ids() -> (  # noqa: E501
    None
):
    with pytest.raises(DomainMapperDerivationError) as exc_info:
        parse_capability_reuse_verdict(
            _reject_payload(confidence=1.5), _OBLIGATION_NODE_ID, _EXISTING_CAPABILITY_ID
        )
    assert _OBLIGATION_NODE_ID in str(exc_info.value)
    assert _EXISTING_CAPABILITY_ID in str(exc_info.value)
