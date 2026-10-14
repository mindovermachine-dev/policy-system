"""Tests for `scripts/check_doc_links.py`.

The check reads markdown documents and reports broken relative links, missing heading anchors
and backticked repository paths that do not exist. Tests build real repositories in `tmp_path`
(including a real `git init` for the tracked mode), inject one violation per rule and run the
script's `main`; nothing is mocked.
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
SCRIPT_PATH = REPO_ROOT / "scripts" / "check_doc_links.py"
GIT_TIMEOUT_SECONDS = 30

GUIDE = "# Guide\n\n## Setup steps\n\nText.\n"
INDEX = (
    "# Index\n\nSee [the guide](guide.md) and [setup](guide.md#setup-steps) "
    "and [this heading](#index).\n\nCode lives in `scripts/tool.py`.\n"
)


def _load_script() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location("check_doc_links", SCRIPT_PATH)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        message = f"could not load spec for {SCRIPT_PATH}"
        raise ImportError(message)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


links = _load_script()


def _write(root: Path, relative: str, content: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


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
    """A git repository whose documents all resolve."""
    _write(tmp_path, "docs/guide.md", GUIDE)
    _write(tmp_path, "docs/index.md", INDEX)
    _write(tmp_path, "scripts/tool.py", "VALUE = 1\n")
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "add", "-A")
    return tmp_path


def _run_tracked(root: Path, capsys: pytest.CaptureFixture[str]) -> tuple[int, str]:
    code = links.main(["--root", str(root), "--tracked"])
    return code, capsys.readouterr().out


def _add_and_run(
    repo: Path, capsys: pytest.CaptureFixture[str], relative: str, content: str
) -> tuple[int, str]:
    _write(repo, relative, content)
    _git(repo, "add", "-A")
    return _run_tracked(repo, capsys)


def test_clean_documents_exit_zero_and_print_nothing(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _run_tracked(repo, capsys) == (0, "")


def test_link_to_a_missing_file_fails_naming_the_rule(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out = _add_and_run(repo, capsys, "docs/bad.md", "[gone](missing.md)\n")

    assert code == 1
    assert "RULE doc-link-missing" in out
    assert "docs/bad.md:1" in out
    assert "missing.md" in out
    assert "How to fix" in out


def test_link_to_a_missing_heading_fails_naming_the_rule(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out = _add_and_run(repo, capsys, "docs/bad.md", "[x](guide.md#no-such-heading)\n")

    assert code == 1
    assert "RULE doc-link-anchor" in out
    assert "guide.md#no-such-heading" in out


def test_link_to_a_missing_heading_in_the_same_file_fails(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out = _add_and_run(repo, capsys, "docs/bad.md", "# Title\n\n[x](#absent)\n")

    assert code == 1
    assert "RULE doc-link-anchor" in out


def test_backticked_repository_path_that_does_not_exist_fails_naming_the_rule(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out = _add_and_run(repo, capsys, "docs/bad.md", "Run `scripts/gone.py` first.\n")

    assert code == 1
    assert "RULE doc-path-missing" in out
    assert "scripts/gone.py" in out


def test_backticked_glob_and_placeholder_paths_are_patterns_not_references(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    text = "See `scripts/*.sh` and `docs/<name>/readme.md`.\n"

    assert _add_and_run(repo, capsys, "docs/ok.md", text) == (0, "")


def test_external_links_and_fenced_code_are_not_checked(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    text = "[site](https://example.com/missing)\n\n```\n[x](missing.md) `scripts/gone.py`\n```\n"

    assert _add_and_run(repo, capsys, "docs/ok.md", text) == (0, "")


def test_repeated_headings_get_numbered_anchors(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(repo, "docs/twice.md", "# A\n\n## Same\n\n## Same\n")

    assert _add_and_run(repo, capsys, "docs/ok.md", "[x](twice.md#same-1)\n") == (0, "")


@pytest.mark.parametrize(
    "excluded",
    [
        "docs/audits/report.md",
        "docs/regulations/rule.md",
        "docs/templates/template.md",
        "test-data/sample.md",
        ".claude/skills/demo/assets/example.md",
    ],
)
def test_documents_in_excluded_locations_are_not_checked(
    repo: Path, capsys: pytest.CaptureFixture[str], excluded: str
) -> None:
    assert _add_and_run(repo, capsys, excluded, "[gone](missing.md) `scripts/gone.py`\n") == (0, "")


def test_listed_files_are_checked_without_git(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(tmp_path, "docs/bad.md", "[gone](missing.md)\n")

    code = links.main(["--root", str(tmp_path), "docs/bad.md", "docs/deleted.md", "notes.txt"])

    assert code == 1
    assert "RULE doc-link-missing" in capsys.readouterr().out


def test_listed_file_in_an_excluded_location_is_skipped(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(tmp_path, "docs/templates/t.md", "[gone](missing.md)\n")

    assert links.main(["--root", str(tmp_path), "docs/templates/t.md"]) == 0
    assert capsys.readouterr().out == ""


def test_neither_mode_nor_files_is_a_usage_error(tmp_path: Path) -> None:
    assert links.main(["--root", str(tmp_path)]) == 2


def test_tracked_mode_outside_a_git_repository_is_an_environment_error(tmp_path: Path) -> None:
    assert links.main(["--root", str(tmp_path), "--tracked"]) == 2


def test_failure_line_has_the_documented_shape(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _, out = _add_and_run(repo, capsys, "docs/bad.md", "[gone](missing.md)\n")

    assert out.startswith("check_doc_links.py: RULE doc-link-missing: ")
    assert out.rstrip().endswith("See CONTRIBUTING.md#automated-guardrails.")


def test_tracked_documents_of_the_checked_out_tree_have_no_broken_references(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, out = _run_tracked(REPO_ROOT, capsys)

    assert out == ""
    assert code == 0
