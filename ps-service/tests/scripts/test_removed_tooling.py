"""Tests that the retired staged-file hook layer is gone from the tree.

The names under test are assembled from fragments so this file does not itself spell them.
"""

from __future__ import annotations

import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
PROVENANCE_SCRIPT = REPO_ROOT / "scripts" / "check_docstring_provenance.py"
SUBPROCESS_TIMEOUT_SECONDS = 30


def _n(*parts: str) -> str:
    return "".join(parts)


REMOVED_PATHS = [
    _n(".pre-commit", "-config.yaml"),
    _n(".githooks/pre", "-push"),
    _n(".githooks/basedpyright", "-changed.sh"),
    _n("scripts/run_ci", "_guardrails.py"),
    _n("ps-service/tests/release/test_pre", "_push_hook.py"),
    _n("ps-service/tests/scripts/test_run_ci", "_guardrails.py"),
]
HOOK_RUNNER = _n("pre", "-commit")
REMOVED_FLAG = _n("--changed", "-since")


@pytest.mark.parametrize("relative", REMOVED_PATHS)
def test_retired_file_does_not_exist(relative: str) -> None:
    assert not (REPO_ROOT / relative).exists()


def test_hooks_directory_holds_only_the_commit_hook() -> None:
    names = sorted(path.name for path in (REPO_ROOT / ".githooks").iterdir())

    assert names == [HOOK_RUNNER]


def test_hook_runner_is_not_a_dev_dependency() -> None:
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    dev = pyproject["dependency-groups"]["dev"]

    assert not any(str(entry).startswith(HOOK_RUNNER) for entry in dev)


def test_hook_runner_is_absent_from_the_lock_file() -> None:
    lock = (REPO_ROOT / "uv.lock").read_text(encoding="utf-8")

    assert HOOK_RUNNER not in lock
    assert _n("pre", "_commit") not in lock


def test_provenance_check_help_has_no_changed_since_option() -> None:
    result = subprocess.run(  # noqa: S603 - interpreter and repo script, literal arguments
        [sys.executable, str(PROVENANCE_SCRIPT), "--help"],
        capture_output=True,
        text=True,
        timeout=SUBPROCESS_TIMEOUT_SECONDS,
        check=True,
    )

    assert REMOVED_FLAG not in result.stdout
    assert "--tracked" in result.stdout
