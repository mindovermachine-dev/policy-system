"""Tests for `scripts/check_repo_hygiene.py`.

The check inspects what git tracks. Tests build real repositories in `tmp_path`, inject one
violation per rule and run the script's `main`; nothing is mocked.
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
SCRIPT_PATH = REPO_ROOT / "scripts" / "check_repo_hygiene.py"
GIT_TIMEOUT_SECONDS = 30

README = (
    "# Demo\n\n| Path | Purpose |\n| --- | --- |\n"
    "| `docs/` | documentation |\n| `run.sh` | entry point |\n| `README.md` | this file |\n"
    "| `.gitignore` | ignore rules |\n"
)


def _load_script() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location("check_repo_hygiene", SCRIPT_PATH)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        message = f"could not load spec for {SCRIPT_PATH}"
        raise ImportError(message)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


hygiene = _load_script()


def _write(root: Path, relative: str, content: str, *, executable: bool = False) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    if executable:
        path.chmod(0o755)


def _git(root: Path, *args: str) -> None:
    git = shutil.which("git")
    assert git is not None
    env = {
        **os.environ,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.com",
    }
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
    """A git repository in which every rule passes."""
    _write(tmp_path, "README.md", README)
    _write(tmp_path, "docs/guide.md", "# Guide\n")
    _write(tmp_path, "run.sh", "#!/bin/sh\necho ok\n", executable=True)
    _write(tmp_path, ".gitignore", "build/\n*.log\n")
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "add", "-A")
    return tmp_path


def _run(root: Path, capsys: pytest.CaptureFixture[str]) -> tuple[int, str]:
    code = hygiene.main(["--root", str(root)])
    return code, capsys.readouterr().out


def _add(repo: Path, relative: str, content: str, *, executable: bool = False) -> None:
    _write(repo, relative, content, executable=executable)
    _git(repo, "add", "-A")


def test_clean_repository_exits_zero_and_prints_nothing(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _run(repo, capsys) == (0, "")


def test_tracked_backup_file_fails_naming_the_rule(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _add(repo, "docs/guide.md.orig", "old\n")

    code, out = _run(repo, capsys)

    assert code == 1
    assert "RULE tracked-backup-file" in out
    assert "docs/guide.md.orig" in out
    assert "How to fix" in out


def test_executable_without_shebang_fails_naming_the_rule(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _add(repo, "tool.py", "VALUE = 1\n", executable=True)

    code, out = _run(repo, capsys)

    assert code == 1
    assert "RULE exec-without-shebang" in out
    assert "tool.py" in out


def test_executable_with_shebang_passes(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _add(repo, "docs/tool.py", "#!/usr/bin/env python3\n", executable=True)

    assert _run(repo, capsys) == (0, "")


def test_tracked_file_matching_an_ignore_rule_fails_naming_the_rule(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(repo, "docs/run.log", "x\n")
    _git(repo, "add", "-f", "docs/run.log")

    code, out = _run(repo, capsys)

    assert code == 1
    assert "RULE tracked-but-ignored" in out
    assert "docs/run.log" in out


def test_repeated_gitignore_rule_fails_naming_the_rule(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _add(repo, ".gitignore", "build/\n*.log\n# comment\nbuild/\n")

    code, out = _run(repo, capsys)

    assert code == 1
    assert "RULE duplicate-gitignore-rule" in out
    assert ".gitignore:4" in out
    assert "line 1" in out


def test_top_level_entry_without_a_readme_row_fails_naming_the_rule(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _add(repo, "extras/notes.txt", "n\n")

    code, out = _run(repo, capsys)

    assert code == 1
    assert "RULE readme-layout-missing-row" in out
    assert "`extras`" in out


def test_repository_without_a_readme_skips_the_layout_rule(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _git(repo, "rm", "-q", "-f", "README.md")

    assert _run(repo, capsys) == (0, "")


def test_directory_that_is_not_a_repository_is_an_environment_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = hygiene.main(["--root", str(tmp_path)])

    assert code == 2
    assert "check_repo_hygiene.py:" in capsys.readouterr().err


def test_the_checked_out_repository_is_clean(capsys: pytest.CaptureFixture[str]) -> None:
    assert _run(REPO_ROOT, capsys) == (0, "")
