"""Tests for ps_service.domain_mapper.models."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from ps_service.domain_mapper.models import (
    CapabilityDecision,
    DerivationResult,
    ExtractionResult,
    ExtractionUnit,
    ObligationAssignment,
    RequirementCandidate,
)


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


# --- ExtractionUnit ---------------------------------------------------


def test_extraction_unit_mutation_raises() -> None:
    unit = ExtractionUnit(
        citation_ref="Art. 13(1)",
        text="Manufacturers shall...",
        article_number="13",
        paragraph_number="1",
        article_heading="Obligations of manufacturers",
    )
    with pytest.raises(AttributeError):
        unit.text = "changed"  # type: ignore[misc]


# --- RequirementCandidate ----------------------------------------------


def test_requirement_candidate_mutation_raises() -> None:
    candidate = _candidate()
    with pytest.raises(ValidationError):
        candidate.confidence = 0.5  # type: ignore[misc]


def test_requirement_candidate_rejects_confidence_above_one() -> None:
    with pytest.raises(ValidationError):
        _candidate(confidence=1.5)


def test_requirement_candidate_rejects_confidence_below_zero() -> None:
    with pytest.raises(ValidationError):
        _candidate(confidence=-0.1)


def test_requirement_candidate_accepts_confidence_boundary_values() -> None:
    assert _candidate(confidence=0.0).confidence == 0.0
    assert _candidate(confidence=1.0).confidence == 1.0


def test_requirement_candidate_rejects_empty_role_name() -> None:
    with pytest.raises(ValidationError):
        _candidate(role_name="")


def test_requirement_candidate_rejects_empty_text() -> None:
    with pytest.raises(ValidationError):
        _candidate(text="")


# --- ExtractionResult ----------------------------------------------------


def test_extraction_result_mutation_raises() -> None:
    result = ExtractionResult(
        regulatory_instrument_id="CRA-1.0",
        role_node_ids={},
        requirement_ids=(),
        candidate_count=0,
        skipped_unit_count=0,
        requirement_id_collisions=(),
    )
    with pytest.raises(AttributeError):
        result.candidate_count = 1  # type: ignore[misc]


def test_extraction_result_constructs_with_empty_collisions_in_zero_collision_case() -> None:
    result = ExtractionResult(
        regulatory_instrument_id="CRA-1.0",
        role_node_ids={"Manufacturer": "role_manufacturer_a1b2c3"},
        requirement_ids=("CRA-1.0_req_art_13.1",),
        candidate_count=1,
        skipped_unit_count=0,
        requirement_id_collisions=(),
    )
    assert result.requirement_id_collisions == ()


# --- ObligationAssignment -------------------------------------------------


def test_obligation_assignment_mutation_raises() -> None:
    assignment = ObligationAssignment(
        requirement_id="CRA-1.0_req_art_13.1",
        role_node_id="role_manufacturer_a1b2c3",
        obligation_node_id="obl_risk_management_a8f3b1",
        obligation_text="Conduct Cybersecurity Risk Assessment",
        confidence=0.85,
    )
    with pytest.raises(ValidationError):
        assignment.confidence = 0.1  # type: ignore[misc]


def test_obligation_assignment_rejects_confidence_above_one() -> None:
    with pytest.raises(ValidationError):
        ObligationAssignment(
            requirement_id="CRA-1.0_req_art_13.1",
            role_node_id="role_manufacturer_a1b2c3",
            obligation_node_id=None,
            obligation_text=None,
            confidence=1.1,
        )


def test_obligation_assignment_rejects_confidence_below_zero() -> None:
    with pytest.raises(ValidationError):
        ObligationAssignment(
            requirement_id="CRA-1.0_req_art_13.1",
            role_node_id="role_manufacturer_a1b2c3",
            obligation_node_id=None,
            obligation_text=None,
            confidence=-0.1,
        )


def test_obligation_assignment_allows_none_obligation_node_id_for_unmatchable() -> None:
    assignment = ObligationAssignment(
        requirement_id="CRA-1.0_req_art_13.1",
        role_node_id="role_manufacturer_a1b2c3",
        obligation_node_id=None,
        obligation_text=None,
        confidence=0.4,
    )
    assert assignment.obligation_node_id is None


# --- CapabilityDecision ----------------------------------------------------


def test_capability_decision_mutation_raises() -> None:
    decision = CapabilityDecision(
        obligation_node_id="obl_risk_management_a8f3b1",
        capability_node_id="cap_security_logging_a8f3b1",
        name="Security Logging",
        description=None,
        confidence=0.7,
    )
    with pytest.raises(ValidationError):
        decision.name = "changed"  # type: ignore[misc]


def test_capability_decision_rejects_confidence_above_one() -> None:
    with pytest.raises(ValidationError):
        CapabilityDecision(
            obligation_node_id="obl_risk_management_a8f3b1",
            capability_node_id="cap_security_logging_a8f3b1",
            name="Security Logging",
            description=None,
            confidence=1.5,
        )


def test_capability_decision_rejects_confidence_below_zero() -> None:
    with pytest.raises(ValidationError):
        CapabilityDecision(
            obligation_node_id="obl_risk_management_a8f3b1",
            capability_node_id="cap_security_logging_a8f3b1",
            name="Security Logging",
            description=None,
            confidence=-0.1,
        )


# --- DerivationResult -------------------------------------------------


def test_derivation_result_mutation_raises() -> None:
    result = DerivationResult(
        regulatory_instrument_id="CRA-1.0",
        obligation_node_ids=(),
        capability_node_ids=(),
        unmatched_requirement_ids=(),
    )
    with pytest.raises(AttributeError):
        result.regulatory_instrument_id = "changed"  # type: ignore[misc]
