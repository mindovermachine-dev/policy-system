"""Content lint: the docs describe the conditional blueprint check, its force flag and weekly run.

The blueprint check runs only when a path in the trigger list changed. Both documents that tell
a reader about the check must say what makes it run, where the list lives, how to force it, that
a skipped run still shows as a passed status (with a logged skip line), and that the weekly run
is the backstop whose failure shows as a red scheduled run. Anchors are quoted text, never line
numbers.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[3]
_ARTIFACTS = _REPO / "docs" / "artifacts"
_DOCS = (
    _ARTIFACTS / "idp-configuration-contract.md",
    _ARTIFACTS / "installation-guide.md",
)

_REQUIRED_STATEMENTS = (
    "runs only when a path listed in `scripts/authentik-blueprint-triggers.txt` changed",
    "`AUTHENTIK_BLUEPRINT_FORCE=1`",
    "`authentik-blueprint-weekly`",
    "a skipped run shows as a passed status, not as skipped",
    "`authentik-blueprint: skipped`",
    "red scheduled run",
)


def _collapsed(path: Path) -> str:
    """Return the text of `path` with every run of whitespace replaced by one space."""
    return " ".join(path.read_text("utf-8").split())


@pytest.mark.parametrize("path", _DOCS, ids=lambda path: path.name)
@pytest.mark.parametrize("statement", _REQUIRED_STATEMENTS)
def test_the_doc_states_how_the_blueprint_check_is_triggered_forced_and_backstopped(
    path: Path, statement: str
) -> None:
    assert statement in _collapsed(path)
