"""Tests that the git commit hook runs exactly the `trunk-worthy` wave and nothing else.

The hook file is read from disk and executed in a throw-away repository with a `gh` stand-in on
`PATH`, so the tests prove the commit is blocked by the wave's exit code alone, whatever is or is
not staged.
"""

from __future__ import annotations

import configparser
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
HOOK_PATH = REPO_ROOT / ".githooks" / "pre-commit"
GITCONFIG_PATH = REPO_ROOT / ".gitconfig"
GATE_COMMAND = "gh insitu run trunk-worthy"
SUBPROCESS_TIMEOUT_SECONDS = 30


def _hook_lines() -> list[str]:
    return [
        line.strip()
        for line in HOOK_PATH.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def test_hook_body_is_set_options_and_the_trunk_worthy_wave_only() -> None:
    assert _hook_lines() == ["set -euo pipefail", GATE_COMMAND]


def test_hook_is_an_executable_bash_script() -> None:
    first_line = HOOK_PATH.read_text(encoding="utf-8").splitlines()[0]

    assert first_line == "#!/usr/bin/env bash"
    assert HOOK_PATH.stat().st_mode & stat.S_IXUSR


def test_gitconfig_points_hooks_path_at_the_githooks_directory() -> None:
    parser = configparser.ConfigParser()
    parser.read(GITCONFIG_PATH, encoding="utf-8")

    assert parser["core"]["hooksPath"] == ".githooks"
    assert HOOK_PATH.parent == REPO_ROOT / ".githooks"


@pytest.fixture
def gate_repo(tmp_path: Path) -> Path:
    """A throw-away git repository holding a copy of the hook and a `gh` stand-in."""
    repo = tmp_path / "repo"
    (repo / ".githooks").mkdir(parents=True)
    git = shutil.which("git")
    assert git is not None
    subprocess.run([git, "init", "-q"], cwd=repo, check=True, timeout=SUBPROCESS_TIMEOUT_SECONDS)  # noqa: S603 - resolved executable, literal argv
    hook = repo / ".githooks" / "pre-commit"
    shutil.copy(HOOK_PATH, hook)
    hook.chmod(hook.stat().st_mode | stat.S_IXUSR)
    shim = tmp_path / "bin" / "gh"
    shim.parent.mkdir()
    shim.write_text('#!/bin/sh\necho "$@" > "$GH_ARGS_FILE"\nexit "$GH_RC"\n', encoding="utf-8")
    shim.chmod(shim.stat().st_mode | stat.S_IXUSR)
    return repo


def _run_hook(repo: Path, *, gate_rc: int) -> tuple[subprocess.CompletedProcess[str], str]:
    args_file = repo.parent / "gh-args"
    env = {
        **os.environ,
        "PATH": f"{repo.parent / 'bin'}{os.pathsep}{os.environ['PATH']}",
        "GH_RC": str(gate_rc),
        "GH_ARGS_FILE": str(args_file),
    }
    result = subprocess.run(  # noqa: S603 - fixed path of the copied hook
        [str(repo / ".githooks" / "pre-commit")],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=SUBPROCESS_TIMEOUT_SECONDS,
    )
    return result, args_file.read_text(encoding="utf-8").strip()


@pytest.mark.parametrize("gate_rc", [0, 1, 3])
def test_hook_exit_code_is_the_wave_exit_code_with_nothing_staged(
    gate_repo: Path, gate_rc: int
) -> None:
    result, args = _run_hook(gate_repo, gate_rc=gate_rc)

    assert result.returncode == gate_rc
    assert args == "insitu run trunk-worthy"
