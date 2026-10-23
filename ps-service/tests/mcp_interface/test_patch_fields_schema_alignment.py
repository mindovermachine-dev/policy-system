"""The `fields` schemas the draft-content tools publish follow the domain schema.

The property names and enum values compared against `patchable_fields` and `enum_values` derive
from the same domain schema as the published models, so that part of each check is partly
circular. The independent guards are the written-out literals: the twelve add-control property
names, the enum order of `type` and `implementation_status`, and the nullable-string shape of
every non-enum property.
"""

from __future__ import annotations

from typing import cast

import pytest

from mcp_interface._input_schema import parameter_object_schema
from ps_service.domain_schema import enum_values, patchable_fields

_ADD_CONTROL_FIELDS = frozenset(
    {
        "description",
        "implementation_status",
        "execution_frequency",
        "last_test_date",
        "next_review_date",
        "evidence_ref",
        "pass_fail_criteria",
        "execution_method",
        "evidence_plan",
        "executor_role",
        "reviewer_role",
        "risk_alignment_rationale",
    }
)
_STANDARD_STATUSES = ["draft", "implemented", "reviewed", "deprecated"]
_CONTROL_STATUSES = ["planned", "implemented", "reviewed", "deprecated"]
_CONTROL_TYPES = ["automated", "manual"]
_NULLABLE_STRING = [{"type": "string"}, {"type": "null"}]

_TOOLS: list[tuple[str, str, dict[str, list[str]]]] = [
    ("update-policy-draft", "Policy", {}),
    ("add-standard-to-draft", "Standard", {"implementation_status": _STANDARD_STATUSES}),
    ("update-standard-draft", "Standard", {"implementation_status": _STANDARD_STATUSES}),
    ("add-control-to-draft", "Control", {"implementation_status": _CONTROL_STATUSES}),
    (
        "update-control-draft",
        "Control",
        {"implementation_status": _CONTROL_STATUSES, "type": _CONTROL_TYPES},
    ),
]


def _published(tool: str) -> dict[str, dict[str, object]]:
    fields = parameter_object_schema(tool, "fields")
    assert fields["additionalProperties"] is False
    return cast("dict[str, dict[str, object]]", fields["properties"])


def test_add_control_to_draft_fields_are_the_control_set_without_type() -> None:
    """add-control-to-draft publishes the Control set narrowed by `type` only."""
    published = set(_published("add-control-to-draft"))

    assert published == _ADD_CONTROL_FIELDS
    assert published == patchable_fields("Control") - {"type"}


@pytest.mark.parametrize(("tool", "label", "enums"), _TOOLS)
def test_published_fields_follow_the_domain_schema(
    tool: str, label: str, enums: dict[str, list[str]]
) -> None:
    """Each tool publishes the schema's names; enums keep schema order, the rest are strings."""
    properties = _published(tool)

    expected_names = patchable_fields(label) - (
        {"type"} if tool == "add-control-to-draft" else set()
    )
    assert set(properties) == expected_names
    for name, schema in properties.items():
        expected = (
            [{"enum": enums[name], "type": "string"}, {"type": "null"}]
            if name in enums
            else _NULLABLE_STRING
        )
        assert schema["anyOf"] == expected, name


@pytest.mark.parametrize(
    ("label", "literal"),
    [("Standard", _STANDARD_STATUSES), ("Control", _CONTROL_STATUSES)],
)
def test_implementation_status_literals_match_the_schema_enum(
    label: str, literal: list[str]
) -> None:
    """The written-out enum order equals the domain schema's."""
    assert list(enum_values(label, "implementation_status")) == literal


def test_control_type_literal_matches_the_schema_enum() -> None:
    """The written-out `type` order equals the domain schema's."""
    assert list(enum_values("Control", "type")) == _CONTROL_TYPES
