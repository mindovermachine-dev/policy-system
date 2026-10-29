"""Tests that the guardrail documentation matches the `.insitu.yml` definitions.

Every command in the guardrails table of CONTRIBUTING.md must equal the command of the check it
names in `.insitu.yml`, the three docs that describe the gate must name the wave command, and the
structural pytest guards listed must match the guard test files on disk, so the documentation
cannot drift from the configuration.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
CONTRIBUTING = (REPO_ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
SECTION_PATTERN = re.compile(r"^## Automated guardrails\n(.*?)^## ", re.DOTALL | re.MULTILINE)
TABLE_ROW = re.compile(r"^\| `([\w-]+)`[^|]*\|[^|]*\| `([^`]+)` +\|$", re.MULTILINE)
GUARD_DIR = "ps-service/tests/guardrails"
GUARD_TEST_PATH = re.compile(rf"{re.escape(GUARD_DIR)}/test_\w+\.py")
GATE_COMMAND = "gh insitu run trunk-worthy"
FULL_GATE_COMMAND = "gh insitu run trunk-worthy test audit"
GATE_DOCS = ("CONTRIBUTING.md", "README.md", ".github/copilot-instructions.md")


def _n(*parts: str) -> str:
    return "".join(parts)


FORBIDDEN = "|".join(
    [
        _n("pre-commit", " run"),
        _n("pre-commit", "-config"),
        _n("pre", "_commit"),
        _n("pre", "-push"),
        _n("basedpyright", "-changed"),
        _n("run_ci", "_guardrails"),
    ]
)


def _text(name: str) -> str:
    return (REPO_ROOT / name).read_text(encoding="utf-8")


def _section() -> str:
    match = SECTION_PATTERN.search(CONTRIBUTING)
    assert match is not None, "CONTRIBUTING.md must contain an 'Automated guardrails' section"
    return match.group(1)


def _insitu_commands() -> dict[str, str]:
    inventory = cast("list[dict[str, Any]]", yaml.safe_load(_text(".insitu.yml"))["inventory"])
    return {check["id"]: check.get("command", "") for check in inventory}


def test_section_is_listed_in_the_table_of_contents() -> None:
    assert "- [Automated guardrails](#automated-guardrails)" in CONTRIBUTING


def test_every_table_command_equals_the_insitu_command_of_its_check() -> None:
    rows = TABLE_ROW.findall(_section())
    commands = _insitu_commands()

    assert rows
    for check_id, command in rows:
        assert commands.get(check_id) == command, check_id


@pytest.mark.parametrize("name", GATE_DOCS)
def test_gate_docs_name_the_wave_command_and_the_other_waves(name: str) -> None:
    text = " ".join(_text(name).split())

    assert GATE_COMMAND in text
    assert FULL_GATE_COMMAND in text


@pytest.mark.parametrize("name", GATE_DOCS)
def test_gate_docs_name_no_retired_tooling(name: str) -> None:
    assert re.search(FORBIDDEN, _text(name)) is None


def test_every_check_run_in_ci_for_the_guardrails_is_listed() -> None:
    section = _section()

    for check_id in ("unused-symbols", "import-layering", "config-drift", "doc-links"):
        assert f"`{check_id}`" in section
    assert "`repo-hygiene`" in section


def test_every_pytest_guard_is_listed_with_an_existing_path() -> None:
    listed = set(GUARD_TEST_PATH.findall(_section()))
    on_disk = {
        path.relative_to(REPO_ROOT).as_posix() for path in (REPO_ROOT / GUARD_DIR).glob("test_*.py")
    }

    assert on_disk, f"no guard test files found under {GUARD_DIR}"
    assert on_disk - listed == set(), "guard test files missing from the section"
    assert listed - on_disk == set(), "section names guard test files that do not exist"
