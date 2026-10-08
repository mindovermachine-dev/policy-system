"""The intake profile applied to the base schema equals the committed intake schema's defs.

Compared as parsed JSON (layout is out of scope here, see `test_intake_generation.py`).
A mismatch means the base schema or the profile is missing a fact; the committed file is
the expected side and is never edited to match (AC-BI-004, AC-BI-005).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import pytest

from ps_service.domain_schema import DOMAIN_SCHEMA, Omit, apply_profile
from ps_service.domain_schema.intake import INTAKE_PROFILE, intake_property_defs

_COMMITTED = (
    Path(__file__).resolve().parents[3]
    / "docs"
    / "artifacts"
    / "schemas"
    / "internal-regulation-intake.v1.schema.json"
)
_DEFS = json.loads(_COMMITTED.read_text(encoding="utf-8"))["$defs"]
_LABELS = (
    "RegulatoryInstrument",
    "Role",
    "Requirement",
    "Obligation",
    "Capability",
    "Policy",
    "Standard",
    "Control",
    "PracticeArea",
    "RiskPath",
)


def _def_name(label: str) -> str:
    return f"{label[0].lower()}{label[1:]}Properties"


@pytest.mark.parametrize("label", _LABELS)
def test_generated_property_def_equals_committed_def(label: str) -> None:
    """AC-BI-004/005: parsed equality per label, including property key order."""
    generated = intake_property_defs(DOMAIN_SCHEMA)[_def_name(label)]
    committed = _DEFS[_def_name(label)]

    assert generated == committed
    generated_properties = cast("dict[str, object]", generated["properties"])
    assert list(generated_properties) == list(committed["properties"])
    assert generated.get("required") == committed.get("required")


def test_defs_are_produced_in_the_committed_order() -> None:
    expected = [_def_name(label) for label in _LABELS]

    assert list(intake_property_defs(DOMAIN_SCHEMA)) == expected


def test_profile_node_order_is_the_committed_node_label_order() -> None:
    assert INTAKE_PROFILE.node_order == _LABELS
    assert _DEFS["nodeLabel"]["enum"] == list(_LABELS)


def test_profile_edge_order_is_the_committed_edge_type_order() -> None:
    assert INTAKE_PROFILE.edge_order is not None
    assert [key[0] for key in INTAKE_PROFILE.edge_order] == _DEFS["edge"]["properties"]["type"][
        "enum"
    ]


def test_profile_omits_status_on_standard_control_and_capability() -> None:
    """F3: `status` is omitted on Standard, Control and Capability in the intake profile."""
    omitted = {
        (op.label, op.property_name) for op in INTAKE_PROFILE.operations if isinstance(op, Omit)
    }

    assert {("Standard", "status"), ("Control", "status"), ("Capability", "status")} <= omitted


def test_profiled_schema_keeps_base_standard_status_untouched() -> None:
    """The base schema keeps Standard `status`; only the profiled view omits it."""
    base = next(node for node in DOMAIN_SCHEMA.nodes if node.label == "Standard")
    profiled = next(
        node
        for node in apply_profile(DOMAIN_SCHEMA, INTAKE_PROFILE).nodes
        if node.label == "Standard"
    )

    assert "status" in {p.name for p in base.properties}
    assert "status" not in {p.name for p in profiled.properties}
