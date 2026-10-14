"""Tests that the repository-owned guardrail scripts are wired into the commit hook and CI.

The hook declarations and the CI inventory are read from their configuration files, so a drift
in either file fails here. The scripts' own behaviour is covered by their dedicated test files.
"""

from __future__ import annotations

import re
import shlex
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
PRECOMMIT_PATH = REPO_ROOT / ".pre-commit-config.yaml"
INSITU_PATH = REPO_ROOT / ".insitu.yml"

SCRIPTS = {
    "config-drift": "scripts/check_config_drift.py",
    "doc-links": "scripts/check_doc_links.py",
    "repo-hygiene": "scripts/check_repo_hygiene.py",
}
CI_FLAGS = {"config-drift": "", "doc-links": " --tracked", "repo-hygiene": ""}


def _yaml(path: Path) -> dict[str, Any]:
    return cast("dict[str, Any]", yaml.safe_load(path.read_text(encoding="utf-8")))


def _hook(hook_id: str) -> dict[str, Any]:
    hooks = [
        hook
        for repo in _yaml(PRECOMMIT_PATH)["repos"]
        for hook in repo["hooks"]
        if hook["id"] == hook_id
    ]
    assert len(hooks) == 1, f"hook {hook_id} must be declared exactly once"
    return cast("dict[str, Any]", hooks[0])


def _insitu_check(check_id: str) -> dict[str, Any]:
    checks = [c for c in _yaml(INSITU_PATH)["inventory"] if c.get("id") == check_id]
    assert len(checks) == 1, f"CI check {check_id} must be declared exactly once"
    return cast("dict[str, Any]", checks[0])


def _wave_checks(wave_id: str) -> list[str]:
    waves = [w for w in _yaml(INSITU_PATH)["waves"] if w.get("id") == wave_id]
    assert len(waves) == 1
    return cast("list[str]", waves[0]["checks"])


@pytest.mark.parametrize("hook_id", sorted(SCRIPTS))
def test_hook_runs_the_script_through_uv(hook_id: str) -> None:
    argv = shlex.split(_hook(hook_id)["entry"])

    assert argv == ["uv", "run", "python", SCRIPTS[hook_id]]
    assert (REPO_ROOT / SCRIPTS[hook_id]).is_file()


@pytest.mark.parametrize("hook_id", sorted(SCRIPTS))
def test_ci_runs_the_script_over_the_whole_tree(hook_id: str) -> None:
    command = _insitu_check(hook_id)["command"].strip()

    assert command == f"uv run python {SCRIPTS[hook_id]}{CI_FLAGS[hook_id]}"


@pytest.mark.parametrize("hook_id", sorted(SCRIPTS))
def test_trunk_worthy_wave_lists_the_check_and_fix_all_does_not(hook_id: str) -> None:
    assert hook_id in _wave_checks("trunk-worthy")
    assert hook_id not in _wave_checks("fix-all")


def test_config_drift_hook_takes_no_file_arguments_and_watches_every_input_area() -> None:
    hook = _hook("config-drift")
    pattern = re.compile(hook["files"])

    assert hook["pass_filenames"] is False
    for staged in (
        "charts/policy-system/values.yaml",
        "docs/artifacts/helm-chart-values-reference.md",
        ".env.example",
        "ps-service/src/ps_service/config/settings.py",
        "scripts/deploy-ps-eval.sh",
    ):
        assert pattern.search(staged), staged
    assert not pattern.search("ps-service/tests/test_x.py")


def test_doc_links_hook_receives_staged_markdown_files_only() -> None:
    hook = _hook("doc-links")
    pattern = re.compile(hook["files"])

    assert hook["pass_filenames"] is True
    assert pattern.search("docs/guide.md")
    assert not pattern.search("scripts/tool.py")


def test_repo_hygiene_hook_runs_on_every_commit_without_file_arguments() -> None:
    hook = _hook("repo-hygiene")

    assert hook["pass_filenames"] is False
    assert hook["always_run"] is True


def test_ci_checks_are_declared_before_the_wave_that_lists_them() -> None:
    inventory_ids = [c.get("id") for c in _yaml(INSITU_PATH)["inventory"]]
    wave_position = inventory_ids.index("pip-audit")

    for check_id in SCRIPTS:
        assert inventory_ids.index(check_id) <= wave_position
