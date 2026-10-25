"""Tests that no operator-facing `--help` screen of a tool or script carries provenance.

The `tools/` and `scripts/` entry points feed `--help` from argparse literals and module
docstrings. The static provenance check cannot see ordinary string literals, so this test renders
every entry point's help (and every subcommand's help) in a subprocess and applies the same
patterns to the whitespace-normalised output.
"""

from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    import types
    from collections.abc import Callable

_REPO = Path(__file__).resolve().parents[3]
_SCRIPT = _REPO / "scripts" / "check_docstring_provenance.py"
_RENDER_TIMEOUT_SECONDS = 120
_MIN_SCREENS = 19
_URL = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)
_SUBCOMMAND_SECTION = re.compile(r"^  \{([\w,.-]+)\}", re.MULTILINE)
_EXTRA_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bthis issue\b", re.IGNORECASE),
    re.compile(r"\bMA\d+\b"),
    re.compile(r"\btracker\b", re.IGNORECASE),
    re.compile(r"\bslice\s*[A-Z0-9]\b", re.IGNORECASE),
)
ENTRY_POINTS: tuple[str, ...] = (
    "tools/company-merge/check_baseline_preconditions.py",
    "tools/company-merge/run_live_merge.py",
    "tools/company-merge/company_merge_similarity_sweep.py",
    "tools/curated-export/export_instrument.py",
    "tools/domain-mapper/exclusion_audit.py",
    "tools/graph-ingestion/find_capability_duplicates.py",
    "tools/graph-ingestion/load_graph.py",
    "tools/graph-ingestion/merge_capabilities.py",
    "tools/graph-query/ps.py",
    "tools/backup-restore-verify/graph_digests.py",
    "tools/backup-restore-verify/seed_content.py",
    "tools/backup-restore-verify/seed_grants.py",
    "tools/backup-restore-verify/seed_state.py",
    "scripts/check_mock_boundaries.py",
    "scripts/grant_access_role_client.py",
    "scripts/measure_skills.py",
    "scripts/check_docstring_provenance.py",
)


def _load_script() -> types.ModuleType:
    """Load the provenance checker by path so its patterns are the single source of truth."""
    spec = importlib.util.spec_from_file_location("check_docstring_provenance", _SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_CHECKER = _load_script()


def _matches(text: str) -> list[str]:
    """Return a label for every provenance pattern that matches the normalised `text`."""
    masked = _URL.sub(" ", text)
    normalised = re.sub(r"\s+", " ", masked)
    found = [
        f"{pattern.pattern_id}: {m.group(0)}"
        for pattern in _CHECKER.PATTERNS
        for m in pattern.regex.finditer(normalised)
    ]
    found.extend(
        f"extra: {m.group(0)}" for extra in _EXTRA_PATTERNS for m in extra.finditer(normalised)
    )
    return found


def _render(args: tuple[str, ...]) -> str:
    """Run `python <args> --help` from the repository root and return stdout."""
    result = subprocess.run(  # noqa: S603 -- fixed interpreter and repo-relative script paths
        [sys.executable, *args, "--help"],
        capture_output=True,
        text=True,
        cwd=_REPO,
        timeout=_RENDER_TIMEOUT_SECONDS,
        check=False,
    )
    assert result.returncode == 0, f"{args} --help exited {result.returncode}: {result.stderr}"
    return result.stdout


def _walk(
    args: tuple[str, ...], render: Callable[[tuple[str, ...]], str]
) -> dict[tuple[str, ...], str]:
    """Render `args` and, depth first, every subcommand listed in its positional section."""
    screens: dict[tuple[str, ...], str] = {}
    pending = [args]
    while pending:
        current = pending.pop()
        if current in screens:
            continue
        text = render(current)
        screens[current] = text
        for group in _SUBCOMMAND_SECTION.findall(text):
            pending.extend((*current, name) for name in group.split(","))
    return screens


def _all_screens() -> dict[tuple[str, ...], str]:
    """Render every entry point and its subcommands."""
    screens: dict[tuple[str, ...], str] = {}
    for entry in ENTRY_POINTS:
        screens.update(_walk((entry,), _render))
    return screens


@pytest.fixture(scope="module")
def screens() -> dict[tuple[str, ...], str]:
    """Rendered help screens keyed by the argument list that produced them."""
    return _all_screens()


@pytest.mark.parametrize("entry", ENTRY_POINTS, ids=ENTRY_POINTS)
def test_entry_point_exists_and_help_is_provenance_free(
    entry: str, screens: dict[tuple[str, ...], str]
) -> None:
    """Every tool and script exists, renders help, and its help screens carry no provenance."""
    assert (_REPO / entry).is_file()
    own = {key: text for key, text in screens.items() if key[0] == entry}
    assert own, f"no help rendered for {entry}"
    for key, text in own.items():
        assert not _matches(text), f"{key}: {_matches(text)}"


def test_every_entry_point_and_subcommand_screen_is_rendered(
    screens: dict[tuple[str, ...], str],
) -> None:
    """The walk covers all entry points plus the subcommand screens of graph_digests and ps."""
    assert len(screens) >= _MIN_SCREENS
    keys = set(screens)
    digests = "tools/backup-restore-verify/graph_digests.py"
    assert (digests, "snapshot") in keys
    assert (digests, "compare") in keys
    assert ("tools/graph-query/ps.py", "cypher") in keys


def test_matcher_flags_each_provenance_shape_including_wrapped_text() -> None:
    """Negative control: the matcher flags every shape, even when the help wrapping splits it."""
    samples = (
        "AC" + "-BI-001",
        "issue " + "#12",
        "issue\n   #12",
        "PLAN" + ".md",
        "spike" + "-3",
        "this " + "issue",
        "M" + "A3",
        ".orchestra" + "tor",
    )
    for sample in samples:
        assert _matches(f"see {sample} for details"), sample
    assert not _matches("Assemble the findings report from cached JSON records.")
