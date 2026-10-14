"""Tests that ruff's `T201` rule stays the gate against stray `print` calls in service code.

The rule is configured in `pyproject.toml`; these tests run the locked ruff binary against
the real configuration, so widening the allow-list to service code fails here and not only in
review. ruff is a locked dev dependency, so nothing here touches the network.
"""

from __future__ import annotations

import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
RUFF_TIMEOUT_SECONDS = 60
PRINT_SOURCE = "print(1)\n"
EXPECTED_PRINT_ALLOWED = {"ps-cli/src/**", "tools/**", "docs/regulations/*.py"}


def _ruff_t201(stdin_filename: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed argv: this interpreter running the locked ruff module
        [
            sys.executable,
            "-m",
            "ruff",
            "check",
            "--stdin-filename",
            stdin_filename,
            "--select",
            "T201",
            "-",
        ],
        input=PRINT_SOURCE,
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        check=False,
        timeout=RUFF_TIMEOUT_SECONDS,
    )


@pytest.mark.parametrize(
    "path",
    [
        "ps-service/src/ps_service/x.py",
        "ps-test-support/src/ps_test_support/x.py",
        "scripts/x.py",
    ],
)
def test_print_in_service_and_script_code_fails_naming_the_rule(path: str) -> None:
    result = _ruff_t201(path)

    assert result.returncode == 1
    assert "T201" in result.stdout


@pytest.mark.parametrize("path", ["ps-cli/src/ps_cli/x.py", "tools/x.py"])
def test_print_in_cli_and_tools_is_allowed(path: str) -> None:
    result = _ruff_t201(path)

    assert result.returncode == 0


def test_only_the_expected_trees_may_ignore_the_print_rule() -> None:
    config = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    lint = config["tool"]["ruff"]["lint"]
    per_file: dict[str, list[str]] = lint["per-file-ignores"]

    assert "T201" not in lint.get("ignore", [])
    allowed = {pattern for pattern, codes in per_file.items() if "T201" in codes}
    assert allowed == EXPECTED_PRINT_ALLOWED
