"""Pin the Documentation guidance in the coding-standards documents.

The tests read the real standards files and assert the rule, the rubric, the mandatory subset
and the Python mapping are present, and that the retired summary-only wording is gone.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
STANDARDS = REPO_ROOT / "docs" / "coding-standards"
L1 = STANDARDS / "level1-coding-principles.md"
L2 = STANDARDS / "level2-python-instructions.md"

RUBRIC_ROWS = (
    "Purpose",
    "Summary",
    "Parameters",
    "Return Value",
    "Exceptions",
    "Side Effects",
    "Business Rules",
    "Examples",
    "Accuracy",
    "Readability",
)


def _section(text: str, heading: str) -> str:
    """Return the body of a level-2 `## heading` section, up to the next level-2 heading."""
    match = re.search(
        rf"^## {re.escape(heading)}\n(.*?)(?=^## |\Z)", text, re.DOTALL | re.MULTILINE
    )
    assert match, f"section {heading!r} not found"
    return match.group(1)


def _l1_documentation() -> str:
    return _section(L1.read_text(encoding="utf-8"), "Documentation")


def test_l1_has_documentation_section_with_no_provenance_rule() -> None:
    body = _l1_documentation().lower()
    for banned in ("acceptance-criteria", "issue", "pull request", "plan", "spike", "history"):
        assert banned in body, banned
    assert "no provenance" in body or "never cite" in body


def test_l1_rubric_lists_all_ten_rows() -> None:
    body = _l1_documentation()
    for row in RUBRIC_ROWS:
        assert re.search(rf"^\|\s*{re.escape(row)}\s*\|", body, re.MULTILINE), row
    assert re.search(r"scored 0 .*, 1 .* or 2 ", body)


def _requirement(body: str, name: str) -> str:
    match = re.search(rf"^\|\s*{re.escape(name)}\s*\|\s*([^|]*?)\s*\|", body, re.MULTILINE)
    assert match, name
    return match.group(1)


def test_l1_marks_mandatory_subset() -> None:
    body = _l1_documentation()
    for name in ("Purpose", "Parameters", "Return Value"):
        assert _requirement(body, name) == "Mandatory", name
    for name in ("Exceptions", "Side Effects"):
        assert _requirement(body, name) == "Mandatory when applicable", name
    for name in ("Business Rules", "Examples", "Summary", "Accuracy", "Readability"):
        assert _requirement(body, name) == "If applicable", name


def test_l1_states_guidance_not_hard_gate() -> None:
    body = _l1_documentation().lower()
    assert "guidance" in body
    assert "not a hard gate" in body


def test_l1_states_tests_exemption() -> None:
    body = _l1_documentation().lower()
    assert "tests" in body
    assert "only the no-provenance rule applies to tests" in body
    assert "parameters and return value rows do not apply" in body


def test_l2_maps_args_returns_raises_to_rubric_rows() -> None:
    text = L2.read_text(encoding="utf-8")
    assert re.search(r"^\|\s*Parameters\s*\|.*`Args`", text, re.MULTILINE)
    assert re.search(r"^\|\s*Return Value\s*\|.*`Returns`", text, re.MULTILINE)
    assert re.search(r"^\|\s*Exceptions\s*\|.*`Raises`", text, re.MULTILINE)
    assert "Side Effects" in text
    assert "Business Rules" in text
    assert "```python" in text


def test_one_line_summary_is_enough_wording_absent() -> None:
    hits = [
        path
        for path in STANDARDS.rglob("*")
        if path.is_file()
        and "one-line summary is enough" in path.read_text(encoding="utf-8").lower()
    ]
    assert hits == []


def test_l2_mcp_tool_docstring_rule_consistent() -> None:
    text = L2.read_text(encoding="utf-8")
    assert "**Tool docstrings are client-facing**" in text
    assert "stricter special case of the Documentation rubric" in text
