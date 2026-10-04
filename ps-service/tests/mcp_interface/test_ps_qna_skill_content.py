"""Content lint for `ps-qna/SKILL.md` (issue #109).

Pins that the skill documents the null-status fallback behavior for
entities whose status enum includes `active` but whose `status` property is
null for every matching row (the known pre-#109 legacy-ingestion gap) —
AC-BI-004.
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
