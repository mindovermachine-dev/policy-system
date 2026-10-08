"""#199 golden: schema-derived patchable sets equal fixed expected-value lists."""

from __future__ import annotations

from ps_service.domain_schema import enum_values, patchable_fields

_POLICY_PATCHABLE = frozenset(
    {
        "description",
        "scope_in",
        "scope_out",
        "normative_commitments",
        "review_cadence",
        "exception_pathway",
        "measurable_outcomes",
        "capability_grouping_rationale",
    }
)

_STANDARD_PATCHABLE = frozenset(
    {
        "description",
        "implementation_status",
        "procedure",
        "implementer_role",
        "reviewer_role",
        "applicability_boundary",
        "verification_notes",
        "change_rationale",
    }
)
_CONTROL_PATCHABLE = frozenset(
    {
        "description",
        "implementation_status",
        "type",
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
_STANDARD_STATUSES = ("draft", "implemented", "reviewed", "deprecated")
_CONTROL_STATUSES = ("planned", "implemented", "reviewed", "deprecated")


def test_derived_policy_patchable_fields_equal_the_fixed_list() -> None:
    """#199 AC-BI-002: a new schema property cannot silently become patchable."""
    assert patchable_fields("Policy") == _POLICY_PATCHABLE


def test_derived_standard_patchable_fields_equal_the_fixed_list() -> None:
    """#199 AC-BI-002: Standard excludes title, status and version, keeps implementation_status."""
    assert patchable_fields("Standard") == _STANDARD_PATCHABLE


def test_derived_control_patchable_fields_equal_the_fixed_list() -> None:
    """#199 AC-BI-002: Control excludes title and status, and keeps type patchable."""
    assert patchable_fields("Control") == _CONTROL_PATCHABLE


def test_derived_implementation_status_values_keep_the_schema_order() -> None:
    """#199 AC-BI-003: the status tuples are the schema enums, in order."""
    assert enum_values("Standard", "implementation_status") == _STANDARD_STATUSES
    assert enum_values("Control", "implementation_status") == _CONTROL_STATUSES
