"""Tests for ps_service.domain_mapper.identity."""

from __future__ import annotations

import re

from ps_service.domain_mapper.identity import (
    capability_id,
    control_id,
    obligation_id,
    policy_id,
    requirement_id,
    role_id,
    standard_id,
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
    role = "role_manufacturer_a1b2c3"
    first = obligation_id(role, text)
    second = obligation_id(role, text)
    assert first == second


def test_obligation_id_is_role_scoped() -> None:
    """Same duty text under two different Roles is two distinct Obligation
    nodes — the resolution of issue #42. An Obligation is a weak entity of
    exactly one Role, so `Role -[:HAS]-> Obligation` `1 : 0..*` holds
    structurally rather than by a runtime collision check.
    """
    text = "Report Security Incidents"
    manufacturer_id = obligation_id("role_manufacturer_a1b2c3", text)
    operator_id = obligation_id("role_operator_essential_services_d4e5f6", text)
    assert manufacturer_id != operator_id


def test_obligation_id_slug_is_still_text_only() -> None:
    """The Role enters only the opaque hash, never the human-readable slug —
    mirrors `role_id`'s own shape.
    """
    assert obligation_id("role_manufacturer_a1b2c3", "Report Security Incidents").startswith(
        "obl_report_security_incidents_"
    )


def test_obligation_id_differs_for_different_text() -> None:
    role = "role_manufacturer_a1b2c3"
    first = obligation_id(role, "Conduct Cybersecurity Risk Assessment")
    second = obligation_id(role, "Report Security Incidents")
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


# --- policy_id / standard_id / control_id (issue #54, F4 -- moved from S1) ---
#
# B3 (FLAWS2.md BLOCKER-3): the starting blueprint's worked example ids
# (`pol_engineering_policy_governance_6b0f1a`, ps-domain-concepts.md's
# `pol_data_protection_a8f3b1`) are hand-authored illustrations, not formula
# output -- asserting `policy_id(title) == <hand-authored id>` would fail
# immediately and mislead about what the real function does. Every test below
# asserts shape and composition only, self-consistently derived from the
# functions' own other outputs -- never a hand-authored literal.

_POLICY_ID_SHAPE_RE = re.compile(r"^pol_[a-z0-9_]+_[0-9a-f]{6}$")


def test_policy_id_shape_and_determinism() -> None:
    title = "Data Protection Policy"
    first = policy_id(title)
    second = policy_id(title)

    assert _POLICY_ID_SHAPE_RE.match(first)
    assert first == second


def test_standard_id_is_compositional() -> None:
    """Derived from `policy_id`'s own output, never a magic string."""
    title = "Data Protection Policy"
    pid = policy_id(title)

    assert standard_id(pid, "1") == f"std_{pid}_v1"


def test_control_id_is_compositional() -> None:
    """One level deeper: derived from `standard_id`'s own output."""
    title = "Data Protection Policy"
    sid = standard_id(policy_id(title), "1")

    assert control_id(sid, "automated") == f"ctrl_{sid}_automated"
