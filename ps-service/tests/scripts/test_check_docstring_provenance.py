"""Tests for `scripts/check_docstring_provenance.py`.

The check reads docstrings and comments of Python files and reports provenance text: acceptance
criterion ids, issue and pull-request references, and references to planning artefacts. Tests
use real temporary files and real temporary git repositories, with no mocks. Fixture text that
deliberately contains the banned patterns lives only in ordinary string literals, which the check
itself never scans, so this test module is clean under its own rules.
"""

from __future__ import annotations

import ast
import importlib.util
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    import types

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT_PATH = REPO_ROOT / "scripts" / "check_docstring_provenance.py"
THIS_FILE = Path(__file__).resolve()
GUIDANCE_FILES = (REPO_ROOT / "docs" / "coding-standards" / "level2-python-instructions.md",)
GIT_TIMEOUT_SECONDS = 30


def _load_script() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location("check_docstring_provenance", SCRIPT_PATH)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        message = f"could not load spec for {SCRIPT_PATH}"
        raise ImportError(message)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


cdp = _load_script()


def _found(source: str) -> list[tuple[int, str]]:
    findings = cdp.scan_source(source, "sample.py")
    return [(f.line, f.pattern_id) for f in findings]


def _ids(source: str) -> set[str]:
    return {pattern_id for _, pattern_id in _found(source)}


def _doc(text: str) -> str:
    return f'def f() -> None:\n    """{text}"""\n'


def _write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


# --- detection in docstrings and comments ---------------------------------------------------


def test_flags_ac_id_in_function_docstring() -> None:
    assert _found(_doc("Do the thing (AC-BI-001).")) == [(2, "ac-id")]


def test_flags_ac_id_without_group_segment() -> None:
    assert _ids(_doc("Covers AC-12.")) == {"ac-id"}


def test_flags_ac_id_in_comment() -> None:
    source = "x = 1\n# see AC-BI-003 for details\n"
    assert _found(source) == [(2, "ac-id")]


def test_flags_issue_hash_in_docstring() -> None:
    assert _found(_doc("Fixed in (#123).")) == [(2, "issue-hash")]


def test_flags_issue_hash_with_letter_suffix() -> None:
    assert _ids(_doc("Follow-up to #51a.")) == {"issue-hash"}


@pytest.mark.parametrize("text", ["see issue 163", "issue #7", "GH #9", "ticket 44", "issue_12"])
def test_flags_issue_word_reference(text: str) -> None:
    assert "issue-word" in _ids(_doc(text))


@pytest.mark.parametrize("text", ["merged via PR 12", "PR-12 fixed it", "pull request 3"])
def test_flags_pr_reference(text: str) -> None:
    assert "issue-word" in _ids(_doc(text))


@pytest.mark.parametrize("stem", ["PLAN", "FLAWS", "CHANGES", "HANDOFF", "BASELINE"])
@pytest.mark.parametrize("suffix", ["", ".md"])
def test_flags_planning_doc_names(stem: str, suffix: str) -> None:
    assert "planning-doc" in _ids(_doc(f"Per {stem}{suffix} section four."))


@pytest.mark.parametrize("text", ["see the plan document", "per the handoff file", "hand-off note"])
def test_flags_planning_reference_phrases(text: str) -> None:
    assert "planning-ref" in _ids(_doc(text))


@pytest.mark.parametrize(
    "text", ["see spikes/foo/README", "per the spike document", "spike-3 showed"]
)
def test_flags_spike_path_and_spike_document(text: str) -> None:
    assert "spike-ref" in _ids(_doc(text))


@pytest.mark.parametrize(
    "text",
    ["written to .orchestrator/tracker/x", "inside `.orchestrator`", "see tracker/i240 notes"],
)
def test_flags_orchestrator_directory_reference(text: str) -> None:
    assert "orchestrator-ref" in _ids(_doc(text))


def test_flags_gitignored_planning_document() -> None:
    assert "gitignored-ref" in _ids(_doc("Described in a gitignored planning document."))


def test_flags_module_class_and_attribute_docstrings() -> None:
    source = (
        '"""Module note AC-BI-001."""\n'
        "\n"
        "\n"
        "class C:\n"
        '    """Class note AC-BI-002."""\n'
        "\n"
        "    value: int = 1\n"
        '    """Attribute note AC-BI-003."""\n'
    )
    assert _found(source) == [(1, "ac-id"), (5, "ac-id"), (8, "ac-id")]


def test_reports_exact_line_inside_multiline_docstring() -> None:
    source = (
        'def f() -> None:\n    """Summary.\n\n    More text.\n    Cites AC-BI-001 here.\n    """\n'
    )
    assert _found(source) == [(5, "ac-id")]


def test_reports_comment_line_number() -> None:
    source = "a = 1\nb = 2\n# note AC-BI-001\n"
    assert _found(source) == [(3, "ac-id")]


def test_flags_provenance_in_second_part_of_concatenated_docstring() -> None:
    source = 'def f() -> None:\n    ("First part."\n     " Second part AC-BI-001.")\n'
    assert _found(source) == [(3, "ac-id")]


def test_flags_docstring_after_non_ascii_text_on_same_line() -> None:
    source = 'x = "\u00e9"; "bare AC-BI-001"\n'
    assert _found(source) == [(1, "ac-id")]


def test_finding_format_is_file_line_pattern_text() -> None:
    finding = cdp.scan_source(_doc("Cites AC-BI-001."), "pkg/mod.py")[0]
    assert finding.format() == "pkg/mod.py:2: ac-id: AC-BI-001"


# --- non-flagged text -------------------------------------------------------------------------


def test_ignores_provenance_text_in_assigned_string_literal() -> None:
    assert _found('MESSAGE = "AC-BI-001 and #123 and PLAN.md"\n') == []


def test_ignores_provenance_text_in_call_argument_string() -> None:
    assert _found('print("see AC-BI-001, issue 163, .orchestrator/x")\n') == []


def test_ignores_fixture_text_inside_triple_quoted_assignment() -> None:
    source = 'FIXTURE = """\nSummary AC-BI-001\nissue 163\n"""\n'
    assert _found(source) == []


def test_ignores_external_url_with_fragment_and_numbers() -> None:
    assert _found(_doc("See https://example.com/page#12 and www.example.org/a#3.")) == []


def test_ignores_identifier_ingestion_orchestration() -> None:
    source = (
        "def ingestion_orchestration() -> None:\n"
        '    """Run ingestion_orchestration for each batch."""\n'
        "    # ingestion_orchestration is a pipeline name\n"
        "    ingestion_orchestration_state = 1\n"
    )
    assert _found(source) == []


def test_ignores_orchestrator_attribute_and_bare_word() -> None:
    source = (
        "def f() -> None:\n"
        '    """The orchestrator calls self.orchestrator and orchestrator_client."""\n'
        "    # the orchestrator decides\n"
    )
    assert _found(source) == []


def test_ignores_lowercase_plan_changes_handoff_prose() -> None:
    text = "We plan to apply changes before the handoff token. PLAN_STEPS and CHANGES_SINCE stay."
    assert _found(_doc(text)) == []


def test_ignores_spike_as_load_spike_prose() -> None:
    assert _found(_doc("A latency spike or spikes in load; the cache spiked.")) == []


def test_ignores_issue_without_number_and_priority() -> None:
    assert _found(_doc("The issue_tracker lists issues; priority 3 first.")) == []


def test_ignores_hex_colour_with_letters() -> None:
    assert _found(_doc("Colour #fff and C# and a lone # sign.")) == []


@pytest.mark.parametrize(
    "comment",
    [
        "# noqa: S101",
        "# type: ignore",
        "# pyright: ignore[reportUnknownMemberType]  # lib: untyped stub",
        "#!/usr/bin/env python3",
        "# -*- coding: utf-8 -*-",
    ],
)
def test_ignores_tool_directive_shebang_and_encoding_comments(comment: str) -> None:
    source = f"x = 1  {comment}\n" if comment.startswith("# ") else f"{comment}\nx = 1\n"
    assert _found(source) == []


# --- parse_added_lines ------------------------------------------------------------------------


def test_parse_added_lines_single_line() -> None:
    assert cdp.parse_added_lines("@@ -3,0 +4 @@\n+x\n") == frozenset({4})


def test_parse_added_lines_range() -> None:
    assert cdp.parse_added_lines("@@ -1,2 +10,3 @@\n") == frozenset({10, 11, 12})


def test_parse_added_lines_deletion_only_hunk_is_empty() -> None:
    assert cdp.parse_added_lines("@@ -5,2 +4,0 @@\n-a\n-b\n") == frozenset()


def test_parse_added_lines_merges_several_hunks() -> None:
    diff = "@@ -1 +1 @@\n-a\n+b\n@@ -9,0 +20,2 @@\n+c\n+d\n"
    assert cdp.parse_added_lines(diff) == frozenset({1, 20, 21})


# --- CLI over explicit paths ------------------------------------------------------------------


def test_main_returns_1_and_prints_findings(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = _write(tmp_path / "bad.py", _doc("Cites AC-BI-001."))

    code = cdp.main([str(target)])

    out = capsys.readouterr().out
    assert code == 1
    assert f"{target}:2: ac-id: AC-BI-001" in out
    assert "1 finding(s) in 1 file(s)" in out


def test_main_returns_0_on_clean_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    target = _write(tmp_path / "ok.py", _doc("Adds two numbers."))

    assert cdp.main([str(target)]) == 0
    assert capsys.readouterr().out == ""


def test_main_scans_directory_recursively(tmp_path: Path) -> None:
    _write(tmp_path / "pkg" / "deep" / "bad.py", "# AC-BI-001\n")

    assert cdp.main([str(tmp_path)]) == 1


def test_main_without_paths_or_mode_exits_2(capsys: pytest.CaptureFixture[str]) -> None:
    assert cdp.main([]) == 2
    assert capsys.readouterr().err != ""


def test_main_missing_path_exits_2(tmp_path: Path) -> None:
    assert cdp.main([str(tmp_path / "absent.py")]) == 2


def test_syntax_error_file_exits_2(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    target = _write(tmp_path / "broken.py", "def f(:\n")

    code = cdp.main([str(target)])

    assert code == 2
    assert f"{target}:1: error:" in capsys.readouterr().out


def test_undecodable_file_exits_2(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    target = tmp_path / "latin.py"
    target.write_bytes(b'x = "\xff\xfe"\n')

    assert cdp.main([str(target)]) == 2
    assert f"{target}:1: error:" in capsys.readouterr().out


def test_paths_mode_reports_all_findings_in_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = _write(tmp_path / "many.py", "# AC-BI-001\n# AC-BI-002\n# AC-BI-003\n")

    assert cdp.main([str(target)]) == 1
    assert "3 finding(s) in 1 file(s)" in capsys.readouterr().out


def test_declared_encoding_is_honoured(tmp_path: Path) -> None:
    target = tmp_path / "enc.py"
    target.write_bytes("# -*- coding: latin-1 -*-\n# caf\u00e9 AC-BI-001\n".encode("latin-1"))

    assert cdp.main([str(target)]) == 1


def test_scan_does_not_follow_into_spikes_or_venv(tmp_path: Path) -> None:
    for excluded in ("spikes", ".venv", "node_modules", ".orchestrator"):
        _write(tmp_path / excluded / "bad.py", "# AC-BI-001\n")

    assert cdp.main([str(tmp_path)]) == 0


# --- never imports or executes scanned files --------------------------------------------------


def test_never_imports_or_executes_scanned_file(tmp_path: Path) -> None:
    sentinel = tmp_path / "executed.flag"
    body = (
        "from pathlib import Path\n"
        f"Path({str(sentinel)!r}).write_text('ran')\n"
        "raise RuntimeError('must not run')\n"
        '"""Docstring-like AC-BI-001."""\n'
    )
    target = _write(tmp_path / "hostile.py", body)

    code = cdp.main([str(target)])

    assert code == 1
    assert not sentinel.exists()
    assert not (tmp_path / "__pycache__").exists()


_BANNED_NAME_CALLS = frozenset({"exec", "eval", "compile", "__import__"})
_BANNED_ATTR_CALLS = frozenset({"importlib.import_module", "os.system"})


def _banned_calls(source: str) -> list[str]:
    banned: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        callee = ast.unparse(node.func)
        is_banned_name = isinstance(node.func, ast.Name) and callee in _BANNED_NAME_CALLS
        is_banned_attr = callee in _BANNED_ATTR_CALLS or callee.startswith("runpy.")
        has_shell = any(
            kw.arg == "shell"
            and not (isinstance(kw.value, ast.Constant) and kw.value.value is False)
            for kw in node.keywords
        )
        if is_banned_name or is_banned_attr or has_shell:
            banned.append(callee)
    return banned


def test_scan_module_calls_no_import_or_exec_primitives() -> None:
    assert _banned_calls(SCRIPT_PATH.read_text(encoding="utf-8")) == []


def test_banned_call_detector_flags_each_primitive() -> None:
    hostile = (
        "import importlib, os, runpy, subprocess\n"
        "exec('x')\n"
        "eval('x')\n"
        "compile('x', 'f', 'exec')\n"
        "__import__('os')\n"
        "importlib.import_module('os')\n"
        "runpy.run_path('p')\n"
        "os.system('ls')\n"
        "subprocess.run(['ls'], shell=True)\n"
        "import re\n"
        "re.compile('x')\n"
    )
    assert len(_banned_calls(hostile)) == 8


def test_scan_module_has_no_runtime_import_of_scanned_code() -> None:
    tree = ast.parse(SCRIPT_PATH.read_text(encoding="utf-8"))
    imported = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        (node.module or "").split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    }
    assert "importlib" not in imported
    assert "runpy" not in imported


# --- git helpers ------------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> str:
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
    result = subprocess.run(  # noqa: S603 - git resolved via shutil.which; literal test arguments
        [git, *args],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        timeout=GIT_TIMEOUT_SECONDS,
        check=True,
    )
    return result.stdout


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)
    _git(root, "init", "-q", "-b", "main")
    monkeypatch.chdir(root)
    return root


def _commit(repo: Path, message: str) -> None:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)


LEGACY = '"""Legacy note AC-BI-001."""\n\n\ndef a() -> int:\n    return 1\n'


def test_changed_since_ignores_findings_on_untouched_lines_of_touched_file(repo: Path) -> None:
    _write(repo / "legacy.py", LEGACY)
    _commit(repo, "base")
    _git(repo, "checkout", "-q", "-b", "feature")
    _write(repo / "legacy.py", LEGACY + "\n\ndef b() -> int:\n    return 2\n")
    _commit(repo, "touch legacy")

    assert cdp.main(["--changed-since", "main"]) == 0


def test_changed_since_reports_finding_on_added_line(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(repo / "legacy.py", LEGACY)
    _commit(repo, "base")
    _git(repo, "checkout", "-q", "-b", "feature")
    _write(repo / "legacy.py", LEGACY + "# added AC-BI-009\n")
    _commit(repo, "add comment")

    code = cdp.main(["--changed-since", "main"])

    assert code == 1
    assert "legacy.py:6: ac-id: AC-BI-009" in capsys.readouterr().out


def test_changed_since_scans_only_files_changed_after_merge_base(repo: Path) -> None:
    _write(repo / "legacy.py", LEGACY)
    _commit(repo, "base")
    _git(repo, "checkout", "-q", "-b", "feature")
    _write(repo / "clean.py", _doc("Adds numbers."))
    _commit(repo, "clean change")

    assert cdp.main(["--changed-since", "main"]) == 0


def test_changed_since_ignores_deleted_and_non_python_files(repo: Path) -> None:
    _write(repo / "gone.py", "# AC-BI-001\n")
    _commit(repo, "base")
    _git(repo, "checkout", "-q", "-b", "feature")
    (repo / "gone.py").unlink()
    _write(repo / "notes.md", "AC-BI-001 #12\n")
    _commit(repo, "delete and add markdown")

    assert cdp.main(["--changed-since", "main"]) == 0


def test_changed_since_includes_unstaged_and_staged_changes(repo: Path) -> None:
    _write(repo / "a.py", "x = 1\n")
    _write(repo / "b.py", "y = 1\n")
    _commit(repo, "base")
    _write(repo / "a.py", "x = 1\n# staged AC-BI-001\n")
    _git(repo, "add", "a.py")
    assert cdp.main(["--changed-since", "HEAD"]) == 1
    _git(repo, "reset", "-q", "a.py")
    _git(repo, "checkout", "-q", "--", "a.py")
    _write(repo / "b.py", "y = 1\n# unstaged AC-BI-002\n")

    assert cdp.main(["--changed-since", "HEAD"]) == 1


def test_changed_since_includes_untracked_new_python_file(repo: Path) -> None:
    _write(repo / "base.py", "x = 1\n")
    _commit(repo, "base")
    _write(repo / "fresh.py", "# AC-BI-001\n")

    assert cdp.main(["--changed-since", "HEAD"]) == 1


def test_changed_since_skips_ignored_untracked_files(repo: Path) -> None:
    _write(repo / ".gitignore", "ignored.py\n")
    _write(repo / "base.py", "x = 1\n")
    _commit(repo, "base")
    _write(repo / "ignored.py", "# AC-BI-001\n")

    assert cdp.main(["--changed-since", "HEAD"]) == 0


def test_changed_since_skips_excluded_directories(repo: Path) -> None:
    _write(repo / "base.py", "x = 1\n")
    _commit(repo, "base")
    _write(repo / "spikes" / "s.py", "# AC-BI-001\n")
    _write(repo / ".orchestrator" / "o.py", "# AC-BI-001\n")

    assert cdp.main(["--changed-since", "HEAD"]) == 0


def test_changed_since_unknown_ref_exits_2_with_message(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(repo / "base.py", "x = 1\n")
    _commit(repo, "base")

    code = cdp.main(["--changed-since", "no-such-ref"])

    assert code == 2
    assert "no-such-ref" in capsys.readouterr().err


def test_changed_since_without_commits_exits_2(repo: Path) -> None:
    assert cdp.main(["--changed-since", "HEAD"]) == 2


def test_explicit_path_is_unfiltered_even_with_changed_since(repo: Path) -> None:
    _write(repo / "legacy.py", LEGACY)
    _commit(repo, "base")

    assert cdp.main(["--changed-since", "HEAD", "legacy.py"]) == 1


def test_tracked_mode_scans_every_tracked_python_file(repo: Path) -> None:
    _write(repo / "pkg" / "deep.py", "# AC-BI-001\n")
    _commit(repo, "base")

    assert cdp.main(["--tracked"]) == 1


def test_tracked_mode_passes_when_tracked_files_are_clean(repo: Path) -> None:
    _write(repo / "pkg" / "deep.py", _doc("Adds numbers."))
    _write(repo / "untracked.py", "# AC-BI-001\n")
    _git(repo, "add", "pkg/deep.py")
    _git(repo, "commit", "-q", "-m", "base")

    assert cdp.main(["--tracked"]) == 0


def test_tracked_mode_skips_excluded_directories(repo: Path) -> None:
    _write(repo / "spikes" / "s.py", "# AC-BI-001\n")
    _write(repo / "ok.py", "x = 1\n")
    _commit(repo, "base")

    assert cdp.main(["--tracked"]) == 0


def test_tracked_mode_skips_files_deleted_from_working_tree(repo: Path) -> None:
    _write(repo / "gone.py", "# AC-BI-001\n")
    _commit(repo, "base")
    (repo / "gone.py").unlink()

    assert cdp.main(["--tracked"]) == 0


def test_git_timeout_exits_2(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, repo: Path) -> None:
    shim_dir = tmp_path / "shim"
    shim = shim_dir / "git"
    shim_dir.mkdir()
    shim.write_text("#!/bin/sh\nexec /bin/sleep 5\n", encoding="utf-8")
    shim.chmod(shim.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{shim_dir}{os.pathsep}{os.environ['PATH']}")

    with pytest.raises(cdp.GitError, match="timed out"):
        cdp.run_git(["status"], repo, timeout=0.2)


def test_main_maps_git_failure_to_exit_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path.parent))

    assert cdp.main(["--tracked"]) == 2
    assert capsys.readouterr().err != ""


# --- self-hosting and guidance ----------------------------------------------------------------


def test_check_passes_on_its_own_source_and_its_tests() -> None:
    assert cdp.main([str(SCRIPT_PATH), str(THIS_FILE)]) == 0


def test_guidance_python_examples_carry_no_provenance() -> None:
    fence_open = "```python"
    fence_close = "```"
    examples: list[str] = []
    for guidance in GUIDANCE_FILES:
        lines = guidance.read_text(encoding="utf-8").splitlines()
        current: list[str] | None = None
        for line in lines:
            if current is None and line.strip() == fence_open:
                current = []
            elif current is not None and line.strip() == fence_close:
                examples.append("\n".join(current) + "\n")
                current = None
            elif current is not None:
                current.append(line)

    assert examples, "expected at least one python example in the guidance"
    for example in examples:
        assert cdp.scan_source(example, "guidance.py") == []
