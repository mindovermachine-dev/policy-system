"""Content lint for `ps-restore-instrument/SKILL.md`.

Reads the real SKILL.md (no mocks) and pins what the skill tells the user about
near-miss reviews the restore creates.
"""

from __future__ import annotations

from pathlib import Path

_REPO = Path(__file__).resolve().parents[3]
_SKILL = _REPO / "ps-skills" / "ps-plugin" / "skills" / "ps-restore-instrument" / "SKILL.md"


def _text() -> str:
    return " ".join(_SKILL.read_text(encoding="utf-8").split())


def test_skill_has_frontmatter_name_and_description() -> None:
    raw = _SKILL.read_text(encoding="utf-8")

    assert raw.startswith("---\nname: ps-restore-instrument\ndescription: ")


def test_skill_reports_the_near_miss_review_count() -> None:
    assert "near_miss_reviews_created" in _text()


def test_skill_points_to_the_near_miss_review_skill_for_resolution() -> None:
    assert "ps-near-miss-review" in _text()


def test_skill_states_the_count_is_absent_on_committed_apply_pending() -> None:
    text = _text()

    assert "committed_apply_pending" in text
    assert "omits" in text
