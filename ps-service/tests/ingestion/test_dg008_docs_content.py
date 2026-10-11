"""Content lint: `dataset-gates.md` documents the post-persist gate DG-008 (GH #99).

Anchors are headings and quoted text, never line numbers.
"""

from __future__ import annotations

import re
from pathlib import Path

_REPO = Path(__file__).resolve().parents[3]
_GATES = _REPO / "ps-skills" / "ps-plugin" / "rubrics" / "dataset-gates.md"
_INTAKE = _REPO / "docs" / "artifacts" / "internal-regulation-intake-format.md"


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _section(text: str, heading: str) -> str:
    lines = text.splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip() == heading)
    end = next(
        (i for i in range(start + 1, len(lines)) if re.match(r"^#{1,2} ", lines[i])),
        len(lines),
    )
    return "\n".join(lines[start:end])


def test_gate_table_has_a_dg_008_row_for_the_post_persist_check() -> None:
    row = next(line for line in _text(_GATES).splitlines() if line.startswith("| DG-008"))

    assert "Post-persist" in row
    assert "{short}_baseline" in row
    assert "{short}_native" in row


def test_dg_008_has_its_own_post_persist_section_with_its_failure_status() -> None:
    section = _section(_text(_GATES), "## 4. Post-Persist Gate (DG-008)")

    assert "FAILED_POST_PERSIST_GATE" in section
    assert "after** persist" in section
    assert "outside the pre-export" in section


def test_the_pre_export_order_and_failure_status_cover_dg_001_to_dg_007_only() -> None:
    text = _text(_GATES)
    order = _section(text, "## 3. Execution Order")
    failure = _section(text, "## 2. Failure Handling")

    assert "DG-001 through DG-007" in order
    assert "DG-008" in order  # named only to exclude it
    assert "post-persist" in order
    assert "If any of DG-001 through DG-007 fails" in failure
    assert "FAILED_DATASET_GATE" in failure
    assert "FAILED_POST_PERSIST_GATE" not in failure


def test_the_intake_format_warns_that_a_new_instrument_id_replaces_the_instrument() -> None:
    text = " ".join(_text(_INTAKE).split())

    assert "replaces the instrument rather than adding a second one" in text
    assert "previous version" in text
