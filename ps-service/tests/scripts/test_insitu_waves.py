"""Tests that `.insitu.yml` is the single definition of every check and runs them in three waves.

The inventory, the wave lists, the tool configuration files and the CI workflows are read from
disk, so a drift in any of them fails here without running the tools themselves.
"""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
INSITU_PATH = REPO_ROOT / ".insitu.yml"
PYPROJECT_PATH = REPO_ROOT / "pyproject.toml"
MARKDOWNLINT_CONFIG_PATH = REPO_ROOT / ".markdownlint-cli2.jsonc"
CSPELL_CONFIG_PATH = REPO_ROOT / ".cspell.jsonc"
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"
PREP_RUNNER_ACTION = REPO_ROOT / ".github" / "actions" / "prep-runner" / "action.yml"

TRUNK_WORTHY_CHECKS = [
    "cspell",
    "markdownlint",
    "prettier",
    "verify-domain-concepts-doc",
    "verify-domain-schema-freshness",
    "actionlint",
    "ruff-check",
    "ruff-format",
    "mock-boundary-guardrail",
    "docstring-provenance",
    "unused-symbols",
    "import-layering",
    "config-drift",
    "doc-links",
    "repo-hygiene",
    "helm-lint",
    "helm-unittest",
]
TEST_CHECKS = ["pytest", "basedpyright"]
AUDIT_CHECKS = ["pip-audit", "action-refs"]
SLOW_CHECKS = {"pytest", "basedpyright", "pip-audit", "action-refs"}
GUARDRAIL_CHECKS = [
    "unused-symbols",
    "import-layering",
    "config-drift",
    "doc-links",
    "repo-hygiene",
    "docstring-provenance",
    "mock-boundary-guardrail",
    "verify-domain-schema-freshness",
]
CI_VERIFY = "gh insitu run trunk-worthy test audit"
CI_WORKFLOWS = ("on_dev.yml", "on_ready.yml")
LEGACY_RUNNER = "pre" + "-commit"
SELECTION_FLAGS = ("--changed" + "-since", "--all-files", "PYTHONPATH")


def _yaml(path: Path) -> dict[str, Any]:
    return cast("dict[str, Any]", yaml.safe_load(path.read_text(encoding="utf-8")))


def _pyproject() -> dict[str, Any]:
    return tomllib.loads(PYPROJECT_PATH.read_text(encoding="utf-8"))


def _command(check_id: str) -> str:
    checks = [c for c in _yaml(INSITU_PATH)["inventory"] if c.get("id") == check_id]
    assert len(checks) == 1, f"check {check_id} must be declared exactly once"
    return str(checks[0]["command"]).strip()


def _wave_ids() -> list[str]:
    return [str(w["id"]) for w in _yaml(INSITU_PATH)["waves"]]


def _wave_checks(wave_id: str) -> list[str]:
    waves = [w for w in _yaml(INSITU_PATH)["waves"] if w.get("id") == wave_id]
    assert len(waves) == 1, f"wave {wave_id} must be declared exactly once"
    return cast("list[str]", waves[0]["checks"])


def _jsonc(path: Path) -> dict[str, Any]:
    """Parse a JSONC file: strip whole-line and trailing comments and trailing commas."""
    text = re.sub(r"(?m)^\s*//.*$", "", path.read_text(encoding="utf-8"))
    text = re.sub(r"(?m)(?<=[,\]\[{}\s])//[^\"\n]*$", "", text)
    text = re.sub(r",(\s*[}\]])", r"\1", text)
    return cast("dict[str, Any]", json.loads(text))


def test_waves_trunk_worthy_test_audit_follow_each_other_in_that_order() -> None:
    ids = _wave_ids()
    start = ids.index("trunk-worthy")

    assert ids[start : start + 3] == ["trunk-worthy", "test", "audit"]


@pytest.mark.parametrize(
    ("wave_id", "expected"),
    [
        ("trunk-worthy", TRUNK_WORTHY_CHECKS),
        ("test", TEST_CHECKS),
        ("audit", AUDIT_CHECKS),
    ],
)
def test_wave_lists_exactly_its_checks(wave_id: str, expected: list[str]) -> None:
    assert sorted(_wave_checks(wave_id)) == sorted(expected)


def test_every_wave_check_is_declared_in_the_inventory() -> None:
    declared = {c["id"] for c in _yaml(INSITU_PATH)["inventory"] if "id" in c}

    for wave_id in ("trunk-worthy", "test", "audit"):
        assert set(_wave_checks(wave_id)) <= declared


def test_no_check_command_invokes_the_legacy_hook_runner() -> None:
    for check in _yaml(INSITU_PATH)["inventory"]:
        assert LEGACY_RUNNER not in str(check.get("command", "")), check["id"]


def test_unused_symbols_and_import_layering_run_the_pinned_tools_directly() -> None:
    assert _command("unused-symbols") == "uv run vulture"
    assert _command("import-layering") == "uv run lint-imports"


def test_dev_group_pins_vulture_and_import_linter() -> None:
    dev = _pyproject()["dependency-groups"]["dev"]

    assert "vulture==2.16" in dev
    assert "import-linter==2.3" in dev


@pytest.mark.parametrize(
    ("check_id", "command"),
    [
        ("markdownlint", "npx --no-install markdownlint-cli2"),
        ("cspell", "npx --no-install cspell"),
        ("import-layering", "uv run lint-imports"),
    ],
)
def test_selection_commands_are_bare(check_id: str, command: str) -> None:
    actual = _command(check_id)

    assert actual == command
    assert '"' not in actual
    assert "!" not in actual
    for flag in SELECTION_FLAGS:
        assert flag not in actual


def test_markdownlint_config_carries_the_exclusions_the_command_line_used_to_pass() -> None:
    globs = _jsonc(MARKDOWNLINT_CONFIG_PATH)["globs"]

    for excluded in ("!**/.orchestrator/**", "!CLAUDE.md", "!.github/agents/**"):
        assert excluded in globs


def test_cspell_config_selects_markdown_files_only() -> None:
    files = _jsonc(CSPELL_CONFIG_PATH)["files"]

    assert files
    assert all(pattern.endswith((".md", ".mdx", ".markdown")) for pattern in files)


@pytest.mark.parametrize("check_id", sorted(SLOW_CHECKS))
def test_slow_checks_stay_out_of_trunk_worthy(check_id: str) -> None:
    assert check_id not in _wave_checks("trunk-worthy")


@pytest.mark.parametrize("check_id", GUARDRAIL_CHECKS)
def test_guardrail_checks_run_in_trunk_worthy_and_never_in_fix_all(check_id: str) -> None:
    assert check_id in _wave_checks("trunk-worthy")
    assert check_id not in _wave_checks("fix-all")


@pytest.mark.parametrize("workflow", CI_WORKFLOWS)
def test_ci_workflow_verifies_with_all_three_waves(workflow: str) -> None:
    text = (WORKFLOWS_DIR / workflow).read_text(encoding="utf-8")

    assert f"verify: {CI_VERIFY}" in text
    assert "verify: gh insitu run trunk-worthy\n" not in text


@pytest.mark.parametrize("workflow", CI_WORKFLOWS)
def test_ci_workflow_job_can_post_commit_statuses(workflow: str) -> None:
    document = _yaml(WORKFLOWS_DIR / workflow)
    jobs = [
        job
        for job in document["jobs"].values()
        if CI_VERIFY in yaml.safe_dump(job, default_flow_style=False)
    ]

    assert len(jobs) == 1
    job = jobs[0]
    assert job["permissions"]["statuses"] == "write"
    assert "GH_TOKEN" in yaml.safe_dump(job)


def test_prep_runner_action_runs_the_verify_input() -> None:
    assert "${{ inputs.verify }}" in PREP_RUNNER_ACTION.read_text(encoding="utf-8")
