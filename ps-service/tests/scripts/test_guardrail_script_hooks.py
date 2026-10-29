"""Tests that the repository-owned guardrail scripts are wired into the `trunk-worthy` wave.

The check inventory is read from `.insitu.yml`, so a drift in it fails here. The scripts' own
behaviour is covered by their dedicated test files.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
INSITU_PATH = REPO_ROOT / ".insitu.yml"

SCRIPTS = {
    "config-drift": "scripts/check_config_drift.py",
    "doc-links": "scripts/check_doc_links.py",
    "repo-hygiene": "scripts/check_repo_hygiene.py",
}
CI_FLAGS = {"config-drift": "", "doc-links": " --tracked", "repo-hygiene": ""}


def _yaml(path: Path) -> dict[str, Any]:
    return cast("dict[str, Any]", yaml.safe_load(path.read_text(encoding="utf-8")))


def _insitu_check(check_id: str) -> dict[str, Any]:
    checks = [c for c in _yaml(INSITU_PATH)["inventory"] if c.get("id") == check_id]
    assert len(checks) == 1, f"CI check {check_id} must be declared exactly once"
    return cast("dict[str, Any]", checks[0])


def _wave_checks(wave_id: str) -> list[str]:
    waves = [w for w in _yaml(INSITU_PATH)["waves"] if w.get("id") == wave_id]
    assert len(waves) == 1
    return cast("list[str]", waves[0]["checks"])


@pytest.mark.parametrize("check_id", sorted(SCRIPTS))
def test_script_exists(check_id: str) -> None:
    assert (REPO_ROOT / SCRIPTS[check_id]).is_file()


@pytest.mark.parametrize("check_id", sorted(SCRIPTS))
def test_ci_runs_the_script_over_the_whole_tree(check_id: str) -> None:
    command = _insitu_check(check_id)["command"].strip()

    assert command == f"uv run python {SCRIPTS[check_id]}{CI_FLAGS[check_id]}"


@pytest.mark.parametrize("check_id", sorted(SCRIPTS))
def test_trunk_worthy_wave_lists_the_check_and_fix_all_does_not(check_id: str) -> None:
    assert check_id in _wave_checks("trunk-worthy")
    assert check_id not in _wave_checks("fix-all")


def test_ci_checks_are_declared_before_the_wave_that_lists_them() -> None:
    inventory_ids = [c.get("id") for c in _yaml(INSITU_PATH)["inventory"]]
    wave_position = inventory_ids.index("pip-audit")

    for check_id in SCRIPTS:
        assert inventory_ids.index(check_id) <= wave_position
