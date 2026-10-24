"""Content lint for `ps-qna/SKILL.md`.

Pins that the skill documents the null-status fallback behavior for
entities whose status enum includes `active` but whose `status` property is
null for every matching row (the known legacy-ingestion gap).
"""

from __future__ import annotations

from pathlib import Path

_SKILL_PATH = (
    Path(__file__).resolve().parents[3]
    / "ps-skills"
    / "ps-plugin"
    / "skills"
    / "ps-qna"
    / "SKILL.md"
)


def _skill_text() -> str:
    return " ".join(_SKILL_PATH.read_text(encoding="utf-8").split())


def test_documents_null_status_fallback_condition() -> None:
    text = _skill_text()

    assert "Null-status fallback" in text
    assert "null for" in text
    assert "every" in text


def test_documents_fallback_is_relax_and_disclose_not_silent() -> None:
    text = _skill_text()

    assert "relax" in text
    assert "disclose" in text or "Disclose" in text
    assert "never silently" in text


def test_documents_fallback_never_applies_to_mixed_null_and_active_rows() -> None:
    text = _skill_text()

    assert "mixed population" in text


def test_filters_output_line_documents_fallback_disclosure_example() -> None:
    text = _skill_text()

    assert "filter relaxed" in text


def test_guardrails_section_names_null_status_fallback() -> None:
    text = _skill_text()

    assert "The null-status fallback" in text


def test_active_only_default_lists_the_capability_status_enum_without_a_merge_value() -> None:
    text = _skill_text()

    assert "Capability (active\\|deprecated) —" in text
    assert "deprecated\\|merged" not in text


def test_active_only_default_explains_an_absorbed_capability_is_deleted() -> None:
    text = _skill_text()

    assert "tombstone" not in text
    assert "is deleted, so a Capability query never returns it" in " ".join(text.split())
    assert "`MergedCapability` node" in text
    assert "merge history" in text


def test_active_only_default_names_the_resolution_marker_and_when_to_read_it() -> None:
    text = _skill_text()

    assert "`ResolvedCapability` node (`id`, `resolves_to`)" in text
    assert "automatic resolution" in text
    assert "resolution history" in text
