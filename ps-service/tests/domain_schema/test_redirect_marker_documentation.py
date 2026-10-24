"""Every document passage that lists the redirect markers names `ResolvedCapability` too.

`REDIRECT_MARKER_LABELS` in `domain_schema/vocabulary_exceptions.py` holds the `Merged<Kind>`
markers a cleanup merge leaves and the `ResolvedCapability` marker Company Merge leaves for a
semantic Capability resolution. The passages pinned here enumerate that set, an exclusion set
built from it, what Company Merge follows, or how a raw-Cypher reader should treat the markers,
so each must name the resolution marker as well. Passages that describe what one cleanup merge,
unmerge or near-miss merge writes per kind are not pinned: those never write or read a
`ResolvedCapability`.

Each passage is found by quoted anchor text, never by line number. A passage is the paragraph,
list item or table row holding the anchor.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[3]
_CA = _REPO / "docs" / "architecture" / "ps-service-container-architecture.md"
_SA = _REPO / "docs" / "architecture" / "ps-solution-architecture.md"
_DC_DOCS = _REPO / "docs" / "artifacts" / "ps-domain-concepts.md"
_DC_PACKAGED = (
    _REPO / "ps-service" / "src" / "ps_service" / "mcp_interface" / "ps-domain-concepts.md"
)
_USER_GUIDE = _REPO / "docs" / "artifacts" / "user-guide.md"
_SKILLS = _REPO / "ps-skills" / "ps-plugin" / "skills"
_QNA = _SKILLS / "ps-qna" / "SKILL.md"
_UNMERGE = _SKILLS / "ps-graph-cleanup" / "references" / "unmerge.md"

_RESOLUTION_MARKER = "ResolvedCapability"
_CAPABILITY_LIFECYCLE = "**Lifecycle:** Either pre-populated as part of a canonical capability"

# A line that opens a new passage: a list item, a numbered item, a table row or a heading.
_PASSAGE_START = re.compile(r"^\s*(?:[-*]\s|\d+\.\s|\||#)")

_PASSAGES = [
    pytest.param(_CA, "- Groups: the merge stage submits one group per write step", id="ca-groups"),
    pytest.param(
        _CA,
        "- A Capability id that matches a `MergedCapability` marker",
        id="ca-capability-redirect",
    ),
    pytest.param(
        _CA,
        "- Serializes per-instrument `{short}_baseline`/`{short}_native` graphs only",
        id="ca-export",
    ),
    pytest.param(_CA, "- A restored baseline never carries a cleanup marker", id="ca-restore"),
    pytest.param(_CA, "| Capability merge markers |", id="ca-cleanup-capability-markers"),
    pytest.param(
        _CA,
        "- Company Merge keeps its add/merge-only contract",
        id="ca-company-merge-contract",
    ),
    pytest.param(_CA, "**Curated content and restore:**", id="ca-curated-content"),
    pytest.param(_CA, "**Readers need no merge filter:**", id="ca-readers"),
    pytest.param(_CA, "**operational labels:**", id="ca-operational-labels"),
    pytest.param(_SA, "| Company Merge | Dedupes and merges", id="sa-company-merge"),
    pytest.param(_DC_DOCS, _CAPABILITY_LIFECYCLE, id="domain-concepts-docs"),
    pytest.param(_DC_PACKAGED, _CAPABILITY_LIFECYCLE, id="domain-concepts-packaged"),
    pytest.param(_USER_GUIDE, "**Merged duplicates and raw Cypher.**", id="user-guide"),
    pytest.param(_QNA, "A `MergedCapability` node", id="ps-qna-skill"),
    pytest.param(_UNMERGE, "It takes only `merged_id`", id="ps-graph-cleanup-unmerge"),
]


def _passages_holding(path: Path, anchor: str) -> list[str]:
    """Every paragraph, list item or table row of `path` whose lines contain `anchor`."""
    passages: list[list[str]] = []
    current: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or (_PASSAGE_START.match(line) and current):
            if current:
                passages.append(current)
            current = []
        if line.strip():
            current.append(line)
    if current:
        passages.append(current)
    return ["\n".join(lines) for lines in passages if anchor in "\n".join(lines)]


@pytest.mark.parametrize(("path", "anchor"), _PASSAGES)
def test_passage_listing_the_merge_markers_names_the_resolution_marker(
    path: Path, anchor: str
) -> None:
    passages = _passages_holding(path, anchor)

    assert passages, f"anchor {anchor!r} not found in {path.relative_to(_REPO)}"
    for passage in passages:
        assert _RESOLUTION_MARKER in passage, passage
