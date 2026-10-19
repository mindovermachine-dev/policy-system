"""Content lint for `ps-list-audit-events/SKILL.md`.

Pins that the skill lists the audit actions a reader can filter by, including the three the role
gate work added (`access.gate`, `passkey.enroll`, `query.cypher`), and that it states what the
trail does not hold (authentication outcomes, passkey rejections).
"""

from __future__ import annotations

from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3] / "ps-skills"
_SKILL = _ROOT / "ps-plugin" / "skills" / "ps-list-audit-events" / "SKILL.md"


def _text(path: Path) -> str:
    return " ".join(path.read_text(encoding="utf-8").split())


def test_the_skill_exists_with_its_name_in_the_frontmatter() -> None:
    assert _SKILL.is_file()
    assert _SKILL.read_text(encoding="utf-8").startswith("---\nname: ps-list-audit-events\n")


def test_the_purpose_lists_the_three_new_actions() -> None:
    text = _text(_SKILL)
    purpose = text[text.index("## Purpose") : text.index("## On Load")]

    for action in ("access.gate", "passkey.enroll", "query.cypher"):
        assert action in purpose, action


def test_the_process_explains_how_to_filter_by_the_new_actions() -> None:
    text = _text(_SKILL)
    process = text[text.index("## Process") : text.index("## Guardrails")]

    assert "`access.gate`" in process
    assert "`passkey.enroll`" in process
    assert "`query.cypher`" in process
    assert "operation" in process  # the resource type of a denial row


def test_the_skill_says_what_the_trail_does_not_hold() -> None:
    text = _text(_SKILL).lower()

    assert "authentication outcomes" in text
    assert "passkey rejections" in text
