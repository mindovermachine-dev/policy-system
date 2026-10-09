"""Unit tests for `scripts/measure_skills.py` (issue #200, AC-BI-001).

Fixture skill trees are written to `tmp_path`, so the tests do not depend on the real skills.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT_PATH = REPO_ROOT / "scripts" / "measure_skills.py"

_spec = importlib.util.spec_from_file_location("measure_skills", SCRIPT_PATH)
assert _spec is not None
assert _spec.loader is not None
measure_skills = importlib.util.module_from_spec(_spec)
sys.modules["measure_skills"] = measure_skills
_spec.loader.exec_module(measure_skills)

_SMALL = "# small\n\n## On Load\n\nA connector that does not expose a `x-tool` tool is not ours.\n"
_BIG = """# big

## On Load

Select the connector, then call the `domain_concepts` tool once.

## Process

Read `references/extra.md` and `rubrics/rubric.md`, and see `elsewhere/outside.md`.
"""


def _plugin(tmp_path: Path) -> Path:
    skills = tmp_path / "plugin" / "skills"
    (skills / "big" / "references").mkdir(parents=True)
    (skills / "small").mkdir()
    (tmp_path / "plugin" / "rubrics").mkdir()
    (skills / "big" / "SKILL.md").write_text(_BIG, encoding="utf-8")
    (skills / "big" / "references" / "extra.md").write_text("x" * 10, encoding="utf-8")
    (tmp_path / "plugin" / "rubrics" / "rubric.md").write_text("y" * 5, encoding="utf-8")
    (skills / "small" / "SKILL.md").write_text(_SMALL, encoding="utf-8")
    return skills


def test_reports_size_references_and_on_load_tool_calls(tmp_path: Path) -> None:
    by_name = {m.name: m for m in measure_skills.measure_all(_plugin(tmp_path))}

    big = by_name["big"]
    assert big.skill_chars == len(_BIG)
    assert big.references == {"rubrics/rubric.md": 5, "skills/big/references/extra.md": 10}
    assert big.unresolved_references == ("elsewhere/outside.md",)
    assert big.on_load_tool_calls == ("domain_concepts",)
    assert by_name["small"].on_load_tool_calls == ()


def test_report_is_identical_on_rerun(tmp_path: Path) -> None:
    skills = _plugin(tmp_path)

    first = measure_skills.render_report(measure_skills.measure_all(skills))
    second = measure_skills.render_report(measure_skills.measure_all(skills))

    assert first == second
    assert first.index("`big`") < first.index("`small`")


def test_coverage_set_is_smallest_set_of_largest_skills(tmp_path: Path) -> None:
    measurements = measure_skills.measure_all(_plugin(tmp_path))

    assert measure_skills.coverage_set(measurements) == ["big"]
    assert measure_skills.coverage_set(measurements, target=0.999) == ["big", "small"]
