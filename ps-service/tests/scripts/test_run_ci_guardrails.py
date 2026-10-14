"""Tests for `scripts/run_ci_guardrails.py`.

The replica reads check commands from an inventory file and runs them against an exported copy
of the working tree. Tests build real repositories in `tmp_path` with an inventory whose commands
are small real shell commands, so nothing is mocked.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    import types

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT_PATH = REPO_ROOT / "scripts" / "run_ci_guardrails.py"
GIT_TIMEOUT_SECONDS = 30

INVENTORY = """\
inventory:
  - id: config-drift
    name: passing check
    command: test -f marker.txt
  - id: doc-links
    name: failing check
    command: |
      echo broken-link-found
      exit 3
  - id: unrelated
    name: not a guardrail
    command: exit 9
"""


def _load_script() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location("run_ci_guardrails", SCRIPT_PATH)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        message = f"could not load spec for {SCRIPT_PATH}"
        raise ImportError(message)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


replica = _load_script()


def _git(root: Path, *args: str) -> None:
    git = shutil.which("git")
    assert git is not None
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}
    subprocess.run(  # noqa: S603 - fixed argv built from literals and a resolved executable
        [git, *args],
        cwd=root,
        env=env,
        check=True,
        capture_output=True,
        timeout=GIT_TIMEOUT_SECONDS,
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A git repository with an inventory and one tracked marker file."""
    (tmp_path / ".insitu.yml").write_text(INVENTORY, encoding="utf-8")
    (tmp_path / "marker.txt").write_text("present\n", encoding="utf-8")
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "add", "-A")
    return tmp_path


def test_passing_check_exits_zero_and_reports_id_exit_code_and_seconds(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = replica.main(["--root", str(repo), "--ids", "config-drift"])

    assert code == 0
    assert "config-drift: exit 0 in " in capsys.readouterr().out


def test_failing_check_makes_the_replica_exit_nonzero_and_names_the_check(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = replica.main(["--root", str(repo), "--ids", "config-drift", "doc-links"])

    out = capsys.readouterr().out
    assert code == 1
    assert "config-drift: exit 0" in out
    assert "doc-links: exit 3" in out
    assert "FAILED" in out


def test_default_selection_runs_only_declared_guardrail_ids(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = replica.main(["--root", str(repo)])

    out = capsys.readouterr().out
    assert code == 1
    assert "unrelated" not in out
    assert "config-drift" in out
    assert "doc-links" in out


def test_unknown_explicit_id_is_a_usage_error(repo: Path) -> None:
    assert replica.main(["--root", str(repo), "--ids", "no-such-check"]) == 2


def test_missing_inventory_is_an_environment_error(tmp_path: Path) -> None:
    assert replica.main(["--root", str(tmp_path)]) == 2


def test_uncommitted_edits_are_part_of_the_exported_tree(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (repo / "marker.txt").unlink()
    (repo / "marker.txt").write_text("edited\n", encoding="utf-8")
    inventory = (
        (repo / ".insitu.yml")
        .read_text(encoding="utf-8")
        .replace("test -f marker.txt", "grep -q edited marker.txt")
    )
    (repo / ".insitu.yml").write_text(inventory, encoding="utf-8")

    code = replica.main(["--root", str(repo), "--ids", "config-drift"])

    assert code == 0
    assert "config-drift: exit 0" in capsys.readouterr().out


def test_ignored_files_are_not_exported(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (repo / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")
    (repo / "ignored.txt").write_text("x\n", encoding="utf-8")
    inventory = (
        (repo / ".insitu.yml")
        .read_text(encoding="utf-8")
        .replace("test -f marker.txt", "test ! -e ignored.txt")
    )
    (repo / ".insitu.yml").write_text(inventory, encoding="utf-8")

    assert replica.main(["--root", str(repo), "--ids", "config-drift"]) == 0
    capsys.readouterr()


def test_exported_tree_is_a_git_repository_so_tracked_mode_checks_work(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    inventory = (
        (repo / ".insitu.yml")
        .read_text(encoding="utf-8")
        .replace("test -f marker.txt", "git ls-files --error-unmatch marker.txt")
    )
    (repo / ".insitu.yml").write_text(inventory, encoding="utf-8")

    assert replica.main(["--root", str(repo), "--ids", "config-drift"]) == 0
    capsys.readouterr()
