"""Guard for the `deploy-ps` -> `deploy-ps-prod` rename (issue #165, AC-BI-011 name half).

Marked `integration`: it spawns a real `git ls-files` subprocess (L2 Testing Patterns).
Lists tracked plus untracked-not-ignored files, so it is valid both before and after the
change is staged, and skips paths that no longer exist on disk.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[3]
# Built from parts so this file does not match its own pattern.
LEGACY_NAME = "deploy-ps" + ".sh"
LEGACY_RE = re.compile(re.escape(LEGACY_NAME))
_GIT_TIMEOUT_SECONDS = 30
_EXCLUDED_PREFIXES = ("spikes/", ".orchestrator/")


def _repo_files() -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard"],  # noqa: S607
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
        timeout=_GIT_TIMEOUT_SECONDS,
    )
    files: list[Path] = []
    for line in result.stdout.splitlines():
        if line.startswith(_EXCLUDED_PREFIXES):
            continue
        path = REPO_ROOT / line
        if path.is_file() and path != Path(__file__).resolve():
            files.append(path)
    return files


def test_no_file_content_references_the_legacy_script_name() -> None:
    offenders: list[str] = []
    for path in _repo_files():
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        if LEGACY_RE.search(text):
            offenders.append(str(path.relative_to(REPO_ROOT)))
    assert offenders == [], f"stale references to {LEGACY_NAME}: {offenders}"


def test_legacy_script_file_is_gone() -> None:
    assert not (REPO_ROOT / "scripts" / LEGACY_NAME).exists()


def test_prod_script_exists_executable_with_strict_mode() -> None:
    script = REPO_ROOT / "scripts" / "deploy-ps-prod.sh"
    assert script.is_file()
    assert os.access(script, os.X_OK)
    text = script.read_text(encoding="utf-8")
    assert text.startswith("#!/usr/bin/env bash")
    assert "set -euo pipefail" in text
