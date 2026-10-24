"""The docs describe the near-miss merge batch flow and no one-review merge.

Reads the real markdown and source files off disk; no fakes. A near miss is merged only through
`merge-near-misses`, one passkey approval for the reviews the officer chose, and read back with
`check-near-miss-merges`. The statements below are the ones a reader relies on: which tools exist,
who may call them, what is signed once, what each merge writes, and what `unsettled` means.
"""

from __future__ import annotations

import re
from pathlib import Path

_REPO = Path(__file__).resolve().parents[3]
_USER_GUIDE = _REPO / "docs" / "artifacts" / "user-guide.md"
_OPERATIONS = _REPO / "docs" / "artifacts" / "operations-guide.md"
_README = _REPO / "README.md"
_SKILLS_README = _REPO / "ps-skills" / "readme.md"
_ARCHITECTURE = _REPO / "docs" / "architecture" / "ps-service-container-architecture.md"
_CUC_06 = _REPO / "docs" / "architecture" / "ps-client-use-cases" / "cuc-06-resolve-a-near-miss.md"
_EMIT = _REPO / "ps-service" / "src" / "ps_service" / "audit" / "emit.py"


def _text(path: Path) -> str:
    return " ".join(path.read_text(encoding="utf-8").split())


def _section(path: Path, heading: str) -> str:
    lines = path.read_text(encoding="utf-8").splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip() == heading)
    end = next(
        (i for i in range(start + 1, len(lines)) if re.match(r"#{1,3} ", lines[i])), len(lines)
    )
    return " ".join("\n".join(lines[start:end]).split())


def test_user_guide_describes_the_batch_flow_and_the_status_tool() -> None:
    section = _section(_USER_GUIDE, "### Review near-miss merge candidates")

    for phrase in (
        "`merge-near-misses`",
        "`check-near-miss-merges`",
        "one passkey approval",
        "the reviews you choose",
        "`acknowledged_review_ids`",
        "`merged`",
        "`failed`",
        "`unsettled`",
        "not yet known",
    ):
        assert phrase in section, phrase
    assert "one at a time" in section
    assert "acknowledge_governance_change" not in section


def test_user_guide_audit_table_describes_one_row_per_batch_merge() -> None:
    text = _text(_USER_GUIDE).lower()

    assert "each merge of a batch writes its own `capability.merge` or `role.merge` row" in text


def test_readme_names_the_batch() -> None:
    assert "many near-miss merges under one passkey approval" in _text(_README)
    skills = _text(_SKILLS_README)
    assert "merges a set of chosen near-miss reviews under one passkey approval" in skills


def test_operations_guide_names_the_batch_in_the_signing_failure_row() -> None:
    text = _text(_OPERATIONS)

    assert "a near-miss merge batch included" in text
    assert "a near-miss Capability merge included" not in text


def test_emit_conventions_describe_one_row_per_batch_merge() -> None:
    text = " ".join(_EMIT.read_text(encoding="utf-8").split())

    assert "Passkey-approved near-miss merge batch" in text
    assert "one `capability.merge` or `role.merge` row per merge" in text
    assert "a `failed` row for a merge that did not happen" in text


def test_architecture_lists_the_new_tools_in_the_authorization_table_and_the_action_rows() -> None:
    text = _text(_ARCHITECTURE)

    assert "| `merge-near-misses` | `ComplianceOfficer`, explicit grant | Refused |" in text
    assert "| `check-near-miss-merges` | Real authenticated session;" in text
    assert "| MergeNearMisses |" in text
    assert "| CheckNearMissMerges |" in text
    assert "near_miss_batch.py`" in text
    assert "near_miss_batch_status.py`" in text
    assert "`unsettled`" in text
    assert "| `resolve-near-miss` | None | Accepted | `near_miss_tools.py` |" in text


def test_architecture_no_longer_describes_the_single_merge_entry_point() -> None:
    text = _text(_ARCHITECTURE)

    assert "`resolve-near-miss` (`merge`)" not in text
    assert "resolve-near-miss(merge)" not in text
    assert 'resolve-near-miss(..., "merge")' not in text
    assert "GET /near-misses/approvals" not in text
    assert "`keep-separate` or `merge`" not in text


def test_cuc_06_describes_one_approval_for_a_chosen_set() -> None:
    text = _text(_CUC_06)

    assert "one passkey approval" in text
    assert "`merge-near-misses`" in text
    assert "`check-near-miss-merges`" in text
    assert "chosen set" in text
    assert "`unsettled`" in text
    assert "acknowledg" in text
    assert "`resolve-near-miss`" in text
    assert "(`merge-roles`, same `ComplianceOfficer`" not in text
