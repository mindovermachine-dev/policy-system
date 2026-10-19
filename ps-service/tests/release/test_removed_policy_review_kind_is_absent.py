"""The Policy review kind is gone: no source, test or document gives a review the kind `Policy`.

Company Merge reviews only the node kinds that have a `ReviewableKind` spec (today Capability).
Reads the real files off disk; no fakes. Governance Policy nodes, `GOVERNED_BY`/`OWNS` edges and
the Policy rule of a Capability merge stay: they are not review kinds, so only the review-kind
spellings are gated. Dated findings reports under `docs/audits/` are snapshots and are not scanned.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

_REPO = Path(__file__).resolve().parents[3]
_SERVICE = _REPO / "ps-service"
_SRC = _SERVICE / "src"
_TESTS = _SERVICE / "tests"
_DOC_ROOTS = (_REPO / "docs" / "architecture", _REPO / "docs" / "artifacts", _REPO / "ps-skills")
_IGNORED_PARTS = frozenset({"__pycache__", "spikes"})

_KIND_POLICY = re.compile(
    r"""Literal\[\s*["']Capability["']\s*,\s*["']Policy["']\s*\]|kind\s*=\s*["']Policy["']"""
)

# The four tests of the removed feature: two files and two test functions.
_REMOVED_FILES = (
    _TESTS / "company_merge" / "test_dedup_policy_convergence.py",
    _TESTS / "company_merge" / "test_dedup_offline_policy_convergence.py",
)
_REMOVED_TEST_NAMES = (
    "test_policy_kind_backfills_a_policy_node",
    "test_resolve_capability_convergence_offline_kind_policy_never_needs_an_embedding_caller",
)

# Callables that persist, read or seed a review; none may be handed the kind "Policy".
_REVIEW_CALLABLES = frozenset(
    {
        "persist_pending_reviews",
        "build_pending_review_primitives",
        "read_unresolved_review_pairs",
        "pending_review",
        "seed_near_miss_review",
        "PendingReviewRecord",
    }
)


def _python_files(root: Path) -> list[Path]:
    return [
        path
        for path in sorted(root.rglob("*.py"))
        if not _IGNORED_PARTS & set(path.relative_to(_REPO).parts)
    ]


def _is_policy(node: ast.expr | None) -> bool:
    return isinstance(node, ast.Constant) and node.value == "Policy"


def _call_name(call: ast.Call) -> str:
    func = call.func
    if isinstance(func, ast.Name):
        return func.id
    return func.attr if isinstance(func, ast.Attribute) else ""


def _policy_review_kinds(source: str) -> list[int]:
    """Line numbers where `source` passes "Policy" as a review kind."""
    lines: list[int] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call) and _call_name(node) in _REVIEW_CALLABLES:
            lines.extend(
                node.lineno
                for keyword in node.keywords
                if keyword.arg == "kind" and _is_policy(keyword.value)
            )
        elif isinstance(node, ast.Dict):
            lines.extend(
                node.lineno
                for key, value in zip(node.keys, node.values, strict=True)
                if isinstance(key, ast.Constant) and key.value == "kind" and _is_policy(value)
            )
    return lines


def test_no_policy_review_kind_remains_in_src() -> None:
    offending = [
        f"{path.relative_to(_REPO)}:{number}: {line.strip()[:120]}"
        for path in _python_files(_SRC)
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if _KIND_POLICY.search(line)
    ]

    assert offending == []


def test_removed_policy_kind_tests_are_gone() -> None:
    survivors = [str(path.relative_to(_REPO)) for path in _REMOVED_FILES if path.exists()]
    survivors += [
        f"{path.relative_to(_REPO)}: {name}"
        for path in _python_files(_TESTS)
        if path.name != Path(__file__).name
        for name in _REMOVED_TEST_NAMES
        if f"def {name}(" in path.read_text(encoding="utf-8")
    ]

    assert survivors == []


def test_no_test_passes_policy_as_a_review_kind() -> None:
    offending = [
        f"{path.relative_to(_REPO)}:{number}"
        for path in _python_files(_TESTS)
        if path.name != Path(__file__).name
        for number in _policy_review_kinds(path.read_text(encoding="utf-8"))
    ]

    assert offending == []


def test_the_gate_flags_a_policy_review_kind() -> None:
    assert _KIND_POLICY.search('kind: Literal["Capability", "Policy"],')
    assert _KIND_POLICY.search('dedupe_canonical_nodes(nodes, kind="Policy")')
    assert not _KIND_POLICY.search('label = "Policy"')
    assert _policy_review_kinds('persist_pending_reviews(t, r, kind="Policy")') == [1]
    assert _policy_review_kinds('row = {"id": "r", "kind": "Policy"}') == [1]
    assert _policy_review_kinds("persist_pending_reviews(t, r, kind=CAPABILITY)") == []


# Prose that names Policy as a kind of review or of dedup: "Capability or Policy" dedup, "a Policy
# near miss", "near-miss review for Policies", the retired generic `kind` parameter accepting it.
_POLICY_REVIEW_PROSE = re.compile(
    r"Capabilit(?:y|ies)\s+(?:or|and|/)\s+Polic(?:y|ies)\s+(?:dedup|near|review|convergence)"
    r"|Polic(?:y|ies)\s+(?:near[- ]miss|review\b|dedup|convergence)"
    r"|near[- ]miss(?:es)?\s+review\s+for\s+Polic"
    r"|\bkind\b[^\n.]{0,30}\baccepts\s+Policy"
    r"|test_dedup_(?:offline_)?policy_convergence|kind_policy_never_needs"
)


def _doc_hits(text: str) -> list[int]:
    return [
        number
        for number, line in enumerate(text.splitlines(), 1)
        if _POLICY_REVIEW_PROSE.search(line) or _KIND_POLICY.search(line)
    ]


def test_docs_name_no_policy_review_kind() -> None:
    offending = [
        f"{path.relative_to(_REPO)}:{number}"
        for root in _DOC_ROOTS
        for path in sorted(root.rglob("*.md"))
        if not _IGNORED_PARTS & set(path.relative_to(_REPO).parts)
        for number in _doc_hits(path.read_text(encoding="utf-8"))
    ]

    assert offending == []


def test_the_docs_gate_flags_prose_naming_a_policy_review_kind() -> None:
    assert _doc_hits("Company Merge dedupes Capability and Policy nodes") == []
    assert _doc_hits("It records a Policy near-miss for each pair.") == [1]
    assert _doc_hits("and no near-miss review for Policies.") == [1]
    assert _doc_hits("the generic `kind` parameter still accepts Policy") == [1]
    assert _doc_hits("a Policy is governed by one Capability") == []
