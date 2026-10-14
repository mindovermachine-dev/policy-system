"""Tests that the provenance check is wired into the commit hook and the CI gate.

The hook entry and the CI command are read from their configuration files and executed against
real temporary git repositories, so a drift in either file fails here. The task runner and the
package manager are replaced by a small shell shim that runs the current interpreter; nothing is
mocked and no network is used (the "remote" is a local bare repository).
"""

from __future__ import annotations

import os
import shlex
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT_PATH = REPO_ROOT / "scripts" / "check_docstring_provenance.py"
PRECOMMIT_PATH = REPO_ROOT / ".pre-commit-config.yaml"
INSITU_PATH = REPO_ROOT / ".insitu.yml"
HOOK_ID = "docstring-provenance"
SUBPROCESS_TIMEOUT_SECONDS = 60

CLEAN = (
    '"""Adds numbers."""\n\n\n'
    "def add(a: int, b: int) -> int:\n"
    '    """Add two numbers."""\n'
    "    return a + b\n"
)
VIOLATING = (
    CLEAN
    + "\n\ndef sub(a: int, b: int) -> int:\n    # see AC-BI-001 for context\n    return a - b\n"
)


def _load_yaml(path: Path) -> dict[str, Any]:
    return cast("dict[str, Any]", yaml.safe_load(path.read_text(encoding="utf-8")))


def _hook() -> dict[str, Any]:
    config = _load_yaml(PRECOMMIT_PATH)
    hooks = [hook for repo in config["repos"] for hook in repo["hooks"] if hook["id"] == HOOK_ID]
    assert len(hooks) == 1, "the provenance hook must be declared exactly once"
    return cast("dict[str, Any]", hooks[0])


def _insitu_check() -> dict[str, Any]:
    config = _load_yaml(INSITU_PATH)
    checks = [check for check in config["inventory"] if check.get("id") == HOOK_ID]
    assert len(checks) == 1, "the provenance check must be declared exactly once"
    return cast("dict[str, Any]", checks[0])


def _wave(wave_id: str) -> dict[str, Any]:
    config = _load_yaml(INSITU_PATH)
    waves = [wave for wave in config["waves"] if wave.get("id") == wave_id]
    assert len(waves) == 1
    return cast("dict[str, Any]", waves[0])


def _env(extra_path: Path | None = None) -> dict[str, str]:
    env = {
        **os.environ,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.com",
        "PY": sys.executable,
    }
    if extra_path is not None:
        env["PATH"] = f"{extra_path}{os.pathsep}{env['PATH']}"
    return env


def _run(
    argv: list[str], cwd: Path, env: dict[str, str], *, check: bool = True
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed argv built from literals and resolved executables
        argv,
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=SUBPROCESS_TIMEOUT_SECONDS,
        check=check,
    )


def _git(cwd: Path, *args: str) -> str:
    git = shutil.which("git")
    assert git is not None
    return _run([git, *args], cwd, _env()).stdout


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


@pytest.fixture
def shim_dir(tmp_path: Path) -> Path:
    """Directory holding a `uv` stand-in that runs the script with this interpreter."""
    directory = tmp_path / "shim"
    directory.mkdir()
    uv = directory / "uv"
    uv.write_text('#!/bin/sh\nshift 2\nexec "$PY" "$@"\n', encoding="utf-8")
    uv.chmod(uv.stat().st_mode | stat.S_IXUSR)
    return directory


def _install_script(repo: Path) -> None:
    target = repo / "scripts" / "check_docstring_provenance.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(SCRIPT_PATH, target)


# --- local hook -------------------------------------------------------------------------------


def test_precommit_declares_docstring_provenance_hook() -> None:
    hook = _hook()

    assert "scripts/check_docstring_provenance.py" in hook["entry"]
    assert "--changed-since HEAD" in hook["entry"]
    assert hook["pass_filenames"] is False
    assert "python" in hook["types"]


def test_precommit_hook_excludes_spikes_venv_node_modules_and_orchestrator() -> None:
    import re

    exclude = re.compile(_hook()["exclude"])

    for excluded in ("spikes/a.py", "pkg/.venv/b.py", "node_modules/c.py", ".orchestrator/d.py"):
        assert exclude.search(excluded), excluded
    assert not exclude.search("scripts/check_docstring_provenance.py")


@pytest.fixture
def hook_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "hook_repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _install_script(repo)
    _write(repo / "pkg" / "mod.py", CLEAN)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    return repo


def _run_hook(repo: Path, shim: Path) -> subprocess.CompletedProcess[str]:
    argv = shlex.split(_hook()["entry"])
    return _run(argv, repo, _env(shim), check=False)


def test_hook_entry_fails_for_staged_python_file_with_provenance(
    hook_repo: Path, shim_dir: Path
) -> None:
    _write(hook_repo / "pkg" / "mod.py", VIOLATING)
    _git(hook_repo, "add", "pkg/mod.py")

    result = _run_hook(hook_repo, shim_dir)

    assert result.returncode == 1, result.stderr
    assert "pkg/mod.py:" in result.stdout
    assert "ac-id" in result.stdout


def test_hook_entry_passes_for_staged_clean_python_file(hook_repo: Path, shim_dir: Path) -> None:
    _write(hook_repo / "pkg" / "mod.py", CLEAN + "\n\nVALUE = 1\n")
    _git(hook_repo, "add", "pkg/mod.py")

    result = _run_hook(hook_repo, shim_dir)

    assert result.returncode == 0, result.stdout + result.stderr


def test_hook_entry_ignores_untouched_legacy_violation(hook_repo: Path, shim_dir: Path) -> None:
    _write(hook_repo / "legacy.py", VIOLATING)
    _git(hook_repo, "add", "-A")
    _git(hook_repo, "commit", "-q", "-m", "legacy")
    _write(hook_repo / "pkg" / "mod.py", CLEAN + "\n\nVALUE = 1\n")
    _git(hook_repo, "add", "pkg/mod.py")

    assert _run_hook(hook_repo, shim_dir).returncode == 0


# --- CI gate ----------------------------------------------------------------------------------


def test_insitu_declares_docstring_provenance_check_over_every_tracked_file() -> None:
    command = _insitu_check()["command"]

    assert "scripts/check_docstring_provenance.py" in command
    assert "--tracked" in command
    assert "--changed-since" not in command
    for fetch_step in ("git fetch", "unshallow", "is-shallow-repository"):
        assert fetch_step not in command


def test_insitu_trunk_worthy_includes_docstring_provenance() -> None:
    assert HOOK_ID in _wave("trunk-worthy")["checks"]


def test_insitu_check_not_in_fix_all_wave() -> None:
    assert HOOK_ID not in _wave("fix-all")["checks"]


@pytest.fixture
def ci_repo(tmp_path: Path) -> Path:
    """A repository with one clean tracked file and the script installed."""
    repo = tmp_path / "ci_repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _install_script(repo)
    _write(repo / "pkg" / "mod.py", CLEAN)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    return repo


def _run_ci(repo: Path, shim: Path) -> subprocess.CompletedProcess[str]:
    bash = shutil.which("bash")
    assert bash is not None
    return _run([bash, "-e", "-c", _insitu_check()["command"]], repo, _env(shim), check=False)


def test_insitu_command_passes_for_clean_tracked_tree(ci_repo: Path, shim_dir: Path) -> None:
    result = _run_ci(ci_repo, shim_dir)

    assert result.returncode == 0, result.stdout + result.stderr


def test_insitu_command_fails_for_violation_in_a_tracked_file_untouched_by_the_branch(
    ci_repo: Path, shim_dir: Path
) -> None:
    _write(ci_repo / "legacy.py", VIOLATING)
    _git(ci_repo, "add", "-A")
    _git(ci_repo, "commit", "-q", "-m", "violation already on trunk")

    result = _run_ci(ci_repo, shim_dir)

    assert result.returncode == 1, result.stdout + result.stderr
    assert "legacy.py:" in result.stdout
