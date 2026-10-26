"""Comment guard: the deploy scripts' comments say what the code does, not where it came from.

Acceptance-criterion ids and issue numbers in a comment point at a tracker the reader of the
script does not have, and they go stale as the code changes. The Python provenance checker
covers docstrings only, so this guard covers the shell comments of the deploy scripts and the
owner library they share. Only comment lines are checked: a string literal the code needs (such
as the name of an Authentik blueprint) is not a comment.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPTS = (
    REPO_ROOT / "scripts" / "deploy-ps-eval.sh",
    REPO_ROOT / "scripts" / "deploy-ps-prod.sh",
    REPO_ROOT / "scripts" / "lib" / "authentik-owner.sh",
)
PROVENANCE = re.compile(r"\bAC-[A-Z]+-\d+\b|\b[Ii]ssue #\d+|#\d{2,}\b")
"""An acceptance-criterion id (`AC-`, area letters, digits) or an issue reference (`#`, digits)."""


def _comment_lines(path: Path) -> list[tuple[int, str]]:
    comments: list[tuple[int, str]] = []
    for number, line in enumerate(path.read_text().splitlines(), start=1):
        stripped = line.lstrip()
        if stripped.startswith("#") and not stripped.startswith("#!"):
            comments.append((number, stripped))
    return comments


@pytest.mark.parametrize("path", SCRIPTS, ids=lambda path: path.name)
def test_deploy_script_comments_carry_no_acceptance_criterion_ids_or_issue_numbers(
    path: Path,
) -> None:
    offending = [
        f"{path.name}:{number}: {line}"
        for number, line in _comment_lines(path)
        if PROVENANCE.search(line)
    ]

    assert offending == []
