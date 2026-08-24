"""Tests for ps_service.domain_mapper.identity."""

from __future__ import annotations

from ps_service.domain_mapper.identity import (
    capability_id,
    obligation_id,
    requirement_id,
    role_id,
)

# --- role_id ---------------------------------------------------------


def test_role_id_is_deterministic() -> None:
    first = role_id("Manufacturer", "CRA-1.0")
    second = role_id("Manufacturer", "CRA-1.0")
    assert first == second


def test_role_id_differs_across_regulations_for_same_name() -> None:
    cra_role = role_id("Manufacturer", "CRA-1.0")
    other_role = role_id("Manufacturer", "NIS2-1.0")
    assert cra_role != other_role


# --- requirement_id ----------------------------------------------------


def test_requirement_id_shape_without_letter() -> None:
    assert requirement_id("CRA-1.0", "13", "1", None) == "CRA-1.0_req_art_13.1"


def test_requirement_id_shape_with_letter() -> None:
    assert requirement_id("CRA-1.0", "13", "8", "c") == "CRA-1.0_req_art_13.8c"


# --- obligation_id -------------------------------------------------------


def test_obligation_id_is_deterministic() -> None:
    text = "Conduct Cybersecurity Risk Assessment"
    first = obligation_id(text)
    second = obligation_id(text)
    assert first == second


def test_obligation_id_is_a_pure_function_of_text_alone() -> None:
    """Same text produces the same id regardless of any other context the
    caller might have on hand — B1 fix (PLAN_REVIEWED.md §3.1): no
    role/regulation parameter exists on this function at all."""
    text = "Report Security Incidents"
    manufacturer_context_id = obligation_id(text)
    operator_context_id = obligation_id(text)
    assert manufacturer_context_id == operator_context_id


def test_obligation_id_differs_for_different_text() -> None:
    first = obligation_id("Conduct Cybersecurity Risk Assessment")
    second = obligation_id("Report Security Incidents")
    assert first != second


# --- capability_id -------------------------------------------------------


def test_capability_id_is_deterministic() -> None:
    first = capability_id("Security Logging")
    second = capability_id("Security Logging")
    assert first == second


def test_capability_id_differs_for_different_names() -> None:
    first = capability_id("Security Logging")
    second = capability_id("Data Encryption")
    assert first != second
