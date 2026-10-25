"""Tests that the guardrail section of CONTRIBUTING.md lists commands that really exist.

Every `uv run pre-commit run <id> --all-files` line must name a declared hook, every
`uv run python scripts/<name>.py` line must name a script that exists and is wired into a hook or
a CI check, and the structural pytest guards listed must match the guard test files on disk, so
the documentation cannot drift from the configuration.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, cast

import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
CONTRIBUTING = (REPO_ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
SECTION_PATTERN = re.compile(r"^## Automated guardrails\n(.*?)^## ", re.DOTALL | re.MULTILINE)
HOOK_COMMAND = re.compile(r"uv run pre-commit run ([\w-]+) --all-files")
SCRIPT_COMMAND = re.compile(r"uv run python (scripts/[\w./-]+\.py)")
CI_REPLICA = "scripts/run_ci_guardrails.py"
GUARD_DIR = "ps-service/tests/guardrails"
GUARD_TEST_PATH = re.compile(rf"{re.escape(GUARD_DIR)}/test_\w+\.py")


def _yaml(name: str) -> dict[str, Any]:
    return cast("dict[str, Any]", yaml.safe_load((REPO_ROOT / name).read_text(encoding="utf-8")))


def _section() -> str:
    match = SECTION_PATTERN.search(CONTRIBUTING)
    assert match is not None, "CONTRIBUTING.md must contain an 'Automated guardrails' section"
    return match.group(1)


def _hook_ids() -> set[str]:
    config = _yaml(".pre-commit-config.yaml")
    return {hook["id"] for repo in config["repos"] for hook in repo["hooks"]}


def _ci_commands() -> str:
    return "\n".join(c.get("command", "") for c in _yaml(".insitu.yml")["inventory"])


def test_section_is_listed_in_the_table_of_contents() -> None:
    assert "- [Automated guardrails](#automated-guardrails)" in CONTRIBUTING


def test_every_pre_commit_command_names_a_declared_hook() -> None:
    named = HOOK_COMMAND.findall(_section())

    assert named
    assert set(named) <= _hook_ids()


def test_every_script_command_names_an_existing_script_that_is_wired() -> None:
    scripts = set(SCRIPT_COMMAND.findall(_section())) - {CI_REPLICA}
    hooks = (REPO_ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8")
    ci = _ci_commands()

    assert scripts
    for script in scripts:
        assert (REPO_ROOT / script).is_file(), script
        assert script in hooks or script in ci, script
    assert (REPO_ROOT / CI_REPLICA).is_file()


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
