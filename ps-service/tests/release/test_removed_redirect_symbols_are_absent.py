"""No removed merge-redirect symbol or merged-status value remains in src, skills or docs.

Reads the real files off disk; no fakes. Absorbed nodes are now recorded as `Merged<Kind>` markers
and read through `company_merge/redirects.py`, so the old redirect readers, the Capability
`status='merged'` value, the `coalesce(status, ...)` filters and the `MERGED_INTO` edge must stay
gone. The bare word "merged" is legitimate prose and the public `{"merged": true}` result key, so
only the status-value uses are gated. Allow-listed hits are named below with the reason.
"""

from __future__ import annotations

import re
from pathlib import Path

_REPO = Path(__file__).resolve().parents[3]
_SCANNED = (
    _REPO / "ps-service" / "src",
    _REPO / "ps-skills",
    _REPO / "docs",
)
_IGNORED_PARTS = frozenset({"__pycache__", "spikes"})

_REMOVED_SYMBOLS = re.compile(
    r"read_merge_redirects|MergeRedirects|obligation_redirect|_redirected_resolution|follow_redirect"
    r"|_EMPTY_REDIRECTS|follow_obligation_redirect|apply_obligation_redirects"
    r"|read_obligation_redirects|resolve_obligation_redirects"
)
_STATUS_VALUE = re.compile(
    r"""coalesce\([^)]{0,20}status|['"]merged['"]|status[^|\n]{0,25}merged|merged[^|\n]{0,10}status"""
    r"|MERGED_INTO|(?i:tombstone)"
)
# The public cleanup result key `{"merged": true}` (like `released`), not a status value.
_RESULT_KEY = re.compile(r"""["']merged["']\s*:\s*(True|true)""")
# Dated findings report listing the label and edge set as derived then; it is not maintained.
_DATED_REPORT = _REPO / "docs" / "audits" / "data-persistence-audit.md"


def _scanned_lines() -> list[tuple[Path, int, str]]:
    lines: list[tuple[Path, int, str]] = []
    for root in _SCANNED:
        for path in sorted(root.rglob("*")):
            if not path.is_file() or _IGNORED_PARTS & set(path.relative_to(_REPO).parts):
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            lines.extend((path, number, line) for number, line in enumerate(text.splitlines(), 1))
    return lines


def _offending(line: str, path: Path) -> bool:
    if _REMOVED_SYMBOLS.search(line):
        return True
    if path == _DATED_REPORT or not _STATUS_VALUE.search(line):
        return False
    return not _RESULT_KEY.search(line)


def test_no_removed_symbol_or_status_value_remains() -> None:
    offending = [
        f"{path.relative_to(_REPO)}:{number}: {line.strip()[:120]}"
        for path, number, line in _scanned_lines()
        if _offending(line, path)
    ]

    assert offending == []


def test_the_scan_reads_the_scanned_trees() -> None:
    assert len(_scanned_lines()) > 1000


def test_the_gate_flags_a_removed_symbol_and_a_merged_status_value() -> None:
    sample = Path("sample.py")

    assert _offending("x = read_merge_redirects(graph)", sample)
    assert _offending("WHERE coalesce(c.status, 'active') <> 'merged'", sample)
    assert not _offending('return {"survivor_id": a, "merged": True}', sample)
