"""The one-review near-miss merge path and its status tool are gone; a batch test covers each case.

Reads the real files off disk; no fakes. A near miss is merged only through the batch request, so
the single-merge entry points, the approval-status tool and their helpers must stay out of the
source, the tests, the skills and the docs. The
names are built from fragments so this file does not contain them. Every scenario the single-merge
tests covered has a named batch-of-one test, found here by name.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[3]
_SERVICE = _REPO / "ps-service"
_SCANNED = (_SERVICE / "src", _SERVICE / "tests", _REPO / "ps-skills", _REPO / "docs")
_IGNORED_PARTS = frozenset({"__pycache__", "spikes", "fixtures"})

_REMOVED_NAMES = (
    "_resolve_merge_" + "pending_approval",
    "run_request_" + "near_miss_merge",
    "_request_role_" + "merge",
    "_request_capability_" + "merge",
    "_Merge" + "Request",
    "check-near-miss-" + "approval",
    "check_near_miss_merge_" + "approval",
    "check_near_miss_" + "approval",
)
_REMOVED = re.compile(
    "|".join(re.escape(name) for name in _REMOVED_NAMES) + r"|PendingApproval" + "Status\\b"
)


def _offending_lines(roots: tuple[Path, ...]) -> list[str]:
    found: list[str] = []
    for root in roots:
        for path in sorted(root.rglob("*")):
            if not path.is_file() or _IGNORED_PARTS & set(path.relative_to(_REPO).parts):
                continue
            if path.suffix not in {".py", ".md", ".json", ".yaml", ".toml"}:
                continue
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if _REMOVED.search(line):
                    found.append(f"{path.relative_to(_REPO)}:{number}: {line.strip()[:100]}")
    return found


def test_no_removed_single_merge_symbol_remains_in_the_source_tests_skills_or_docs() -> None:
    assert _offending_lines(_SCANNED) == []


def test_the_scan_covers_the_skills_and_the_docs() -> None:
    assert _REPO / "ps-skills" in _SCANNED
    assert _REPO / "docs" in _SCANNED
    assert (
        _REPO / "ps-skills" / "ps-plugin" / "skills" / "ps-near-miss-review" / "SKILL.md"
    ).is_file()


def test_the_scan_flags_each_removed_name() -> None:
    for name in (*_REMOVED_NAMES, "PendingApproval" + "Status"):
        assert _REMOVED.search(f"x = {name}(review_id)")
    assert not _REMOVED.search("create_near_miss_merge_batch(review_ids=[])")


_SCENARIOS = (
    (
        "stale",
        "graph_cleanup/test_near_miss_batch_validation.py",
        "test_a_review_whose_node_was_absorbed_is_refused_as_stale",
    ),
    (
        "case-2",
        "graph_cleanup/test_near_miss_batch_acknowledgment.py",
        "test_a_case_two_review_without_its_acknowledgment_asks_for_it_naming_review_and_policy",
    ),
    (
        "different-policies",
        "graph_cleanup/test_near_miss_batch_validation.py",
        "test_a_pair_governed_by_two_policies_is_refused_naming_the_release_step_and_the_review",
    ),
    (
        "role-acknowledgment-refused",
        "graph_cleanup/test_near_miss_batch_acknowledgment.py",
        "test_an_acknowledgment_for_a_role_review_is_refused_and_no_approval_is_created",
    ),
    (
        "audit-unavailable",
        "graph_cleanup/test_near_miss_batch_execution.py",
        "test_when_the_audit_trail_cannot_record_a_merge_that_merge_is_not_performed_and_is_reported",
    ),
    (
        "interrupted-signing",
        "graph_cleanup/test_check_near_miss_merge_batch.py",
        "test_an_interrupted_batch_past_the_grace_window_settles_each_merge_and_runs_none",
    ),
)


def _test_names(relative: str) -> set[str]:
    tree = ast.parse((_SERVICE / "tests" / relative).read_text(encoding="utf-8"))
    return {node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)}


@pytest.mark.parametrize(("scenario", "file", "name"), _SCENARIOS, ids=[s[0] for s in _SCENARIOS])
def test_every_removed_single_merge_scenario_has_a_batch_of_one_equivalent(
    scenario: str, file: str, name: str
) -> None:
    assert name in _test_names(file), scenario
