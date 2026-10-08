"""#199 the MCP patch-field validation follows the schema-derived sets and enum order."""

from __future__ import annotations

import pytest

from ps_service.domain_schema import enum_values, patchable_fields
from ps_service.mcp_interface import mcp_server


def test_add_control_to_draft_fields_are_the_control_set_without_type() -> None:
    """#199 AC-BI-007: add-control-to-draft narrows the derived Control set by `type` only."""
    narrowed = mcp_server._ADD_CONTROL_TO_DRAFT_PATCHABLE_FIELDS  # pyright: ignore[reportPrivateUsage]  # pin test reads the tool's private allow-list by design

    assert narrowed == patchable_fields("Control") - {"type"}
    assert narrowed == frozenset(
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


@pytest.mark.parametrize("label", ["Standard", "Control"])
def test_bad_implementation_status_error_lists_the_schema_value_order(label: str) -> None:
    """#199 AC-BI-003: the rejection text enumerates the schema's values in schema order."""
    values = enum_values(label, "implementation_status")
    enum_checks = (
        mcp_server._STANDARD_IMPLEMENTATION_STATUS_VALUES  # pyright: ignore[reportPrivateUsage]  # pin test reads the tool's private enum tuple by design
        if label == "Standard"
        else mcp_server._CONTROL_IMPLEMENTATION_STATUS_VALUES  # pyright: ignore[reportPrivateUsage]  # pin test reads the tool's private enum tuple by design
    )

    with pytest.raises(mcp_server._MalformedPatchFieldsError) as raised:  # pyright: ignore[reportPrivateUsage]  # pin test asserts the private error type by design
        mcp_server._parse_patch_fields(  # pyright: ignore[reportPrivateUsage]  # pin test calls the private validator by design
            {"implementation_status": "bogus"},
            allowed=patchable_fields(label),
            enum_checks={"implementation_status": enum_checks},
        )

    assert str(raised.value) == f"fields.implementation_status must be one of {values} or null"
