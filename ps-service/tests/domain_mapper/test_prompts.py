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
    ExtractionUnit,
    ObligationAssignment,
    RequirementCandidate,
)
from ps_service.domain_mapper.prompts import (
    parse_capability_response,
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
                "text": "Conduct a risk assessment.",
                "type": "requirement",
                "letter_suffix": "a",
                "confidence": 0.9,
            },
            {
                "role_name": "Manufacturer",
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


def test_parse_extraction_response_missing_requirements_key_raises_typed_error() -> None:
    with pytest.raises(DomainMapperExtractionError) as exc_info:
        parse_extraction_response(json.dumps({"unexpected": []}), _UNIT)
    assert "Art. 13(1)" in str(exc_info.value)


def test_parse_extraction_response_item_missing_confidence_raises_typed_error() -> None:
    payload = {
        "requirements": [
            {
                "role_name": "Manufacturer",
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


def test_parse_extraction_response_item_invalid_type_raises_typed_error() -> None:
    payload = {
        "requirements": [
            {
                "role_name": "Manufacturer",
                "text": "Conduct a cybersecurity risk assessment.",
                "type": "not-a-real-type",
                "letter_suffix": None,
                "confidence": 0.5,
            }
        ]
    }

    with pytest.raises(DomainMapperExtractionError):
        parse_extraction_response(json.dumps(payload), _UNIT)


def test_parse_extraction_response_requirements_not_a_list_raises_typed_error() -> None:
    with pytest.raises(DomainMapperExtractionError) as exc_info:
        parse_extraction_response(json.dumps({"requirements": "not-a-list"}), _UNIT)
    assert "Art. 13(1)" in str(exc_info.value)


# --- parse_obligation_response (Increment 11) -------------------------------

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
    unmatchable=true set."""
    payload = json.dumps(
        {"matched_existing_id": None, "new_text": None, "unmatchable": False, "confidence": 0.5}
    )

    with pytest.raises(DomainMapperDerivationError) as exc_info:
        parse_obligation_response(payload, _REQUIREMENT_ID, _ROLE_NODE_ID, _ROLE_VIEW)
    assert _REQUIREMENT_ID in str(exc_info.value)


def test_parse_obligation_response_matched_id_not_in_role_view_raises_typed_error() -> None:
    """A matched_existing_id that doesn't resolve within this Role's own
    registry view (a hallucinated match) is treated the same as a missing
    match — a typed error, not a silently-accepted dangling reference."""
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


# --- parse_capability_response (Increment 13) -------------------------------

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


def test_parse_capability_response_multi_capability_response_returns_two_decisions() -> None:
    """Proves the list-of-decisions shape actually supports >1 -- a single
    Obligation may bundle more than one distinct Capability requirement
    (PLAN_REVIEWED.md §7.4, multi-capability-per-Obligation ported from
    spikes/cellar2/derive_capabilities.py)."""
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
    matched_existing_id nor a new_name value."""
    payload = _capability_payload(
        {"matched_existing_id": None, "new_name": None, "new_description": None, "confidence": 0.5}
    )

    with pytest.raises(DomainMapperDerivationError) as exc_info:
        parse_capability_response(payload, _OBLIGATION_NODE_ID, {})
    assert _OBLIGATION_NODE_ID in str(exc_info.value)


def test_parse_capability_response_matched_id_not_in_registry_raises_typed_error() -> None:
    """A matched_existing_id that doesn't resolve within the given registry
    (a hallucinated match) is treated as malformed, not silently accepted."""
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
