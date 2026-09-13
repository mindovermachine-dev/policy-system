"""Tests for ps_service.company_merge.live_merge_approval.

All fixtures live under `tmp_path` -- no live dependency, and no real
`APPROVAL_LIVE_MERGE_*.md` file is ever written anywhere in this repo by
these tests (per issue #28's own approval-gate design: that file may only
ever be authored by a human).
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

import pytest

from ps_service.company_merge.live_merge_approval import (
    LiveMergeApproval,
    LiveMergeApprovalError,
    load_and_validate_approval,
)

if TYPE_CHECKING:
    from pathlib import Path

_EXPECTED_SCOPE = ("CRA-1.0", "GDPR-1.0", "NIS2-1.0")
_EXPECTED_TARGET_GRAPH = "policy_system"
_INITIAL_PHRASE = "I APPROVE THE LIVE MERGE INTO policy_system"
_RERUN_PHRASE = "I APPROVE THE LIVE MERGE RE-RUN INTO policy_system"

_DEFAULT_FIELDS: dict[str, str] = {
    "approved_by": "tete@cartman.dk",
    "approved_at": "2026-09-13T14:30:00Z",
    "scope": "CRA-1.0,GDPR-1.0,NIS2-1.0",
    "target_graph": "policy_system",
    "precondition_check_result": (
        "cra_baseline: 0 violations - GO; gdpr_baseline: 0 violations - GO; "
        "nis2_baseline: 0 violations - GO"
    ),
    "confirmation_phrase": _INITIAL_PHRASE,
}


def _write_approval_file(tmp_path: Path, **overrides: str | None) -> Path:
    """Write an approval fixture file under `tmp_path`. `None` omits that field."""
    fields = dict(_DEFAULT_FIELDS)
    for key, value in overrides.items():
        if value is None:
            fields.pop(key, None)
        else:
            fields[key] = value
    path = tmp_path / "approval.md"
    path.write_text(
        "\n".join(f"{key}: {value}" for key, value in fields.items()) + "\n",
        encoding="utf-8",
    )
    return path


def test_valid_file_parses_with_all_fields_correct(tmp_path: Path) -> None:
    path = _write_approval_file(tmp_path)

    approval = load_and_validate_approval(
        path,
        expected_scope=_EXPECTED_SCOPE,
        expected_target_graph=_EXPECTED_TARGET_GRAPH,
        run_kind="initial",
    )

    assert approval == LiveMergeApproval(
        approved_by="tete@cartman.dk",
        approved_at="2026-09-13T14:30:00Z",
        scope=("CRA-1.0", "GDPR-1.0", "NIS2-1.0"),
        target_graph="policy_system",
        precondition_check_result=_DEFAULT_FIELDS["precondition_check_result"],
        confirmation_phrase=_INITIAL_PHRASE,
    )


def test_valid_file_with_rerun_phrase_parses_when_validated_as_rerun(
    tmp_path: Path,
) -> None:
    """Confirms the two run_kind phrases are genuinely distinct, not just the rejection case."""
    path = _write_approval_file(tmp_path, confirmation_phrase=_RERUN_PHRASE)

    approval = load_and_validate_approval(
        path,
        expected_scope=_EXPECTED_SCOPE,
        expected_target_graph=_EXPECTED_TARGET_GRAPH,
        run_kind="rerun",
    )

    assert approval.confirmation_phrase == _RERUN_PHRASE


def test_missing_file_raises_error_naming_the_path(tmp_path: Path) -> None:
    missing_path = tmp_path / "does_not_exist.md"

    with pytest.raises(LiveMergeApprovalError, match=re.escape(str(missing_path))):
        load_and_validate_approval(
            missing_path,
            expected_scope=_EXPECTED_SCOPE,
            expected_target_graph=_EXPECTED_TARGET_GRAPH,
            run_kind="initial",
        )


def test_missing_required_field_raises_error_naming_the_field(tmp_path: Path) -> None:
    path = _write_approval_file(tmp_path, approved_by=None)

    with pytest.raises(LiveMergeApprovalError, match="approved_by"):
        load_and_validate_approval(
            path,
            expected_scope=_EXPECTED_SCOPE,
            expected_target_graph=_EXPECTED_TARGET_GRAPH,
            run_kind="initial",
        )


def test_scope_missing_a_regulation_raises_error(tmp_path: Path) -> None:
    path = _write_approval_file(tmp_path, scope="CRA-1.0,NIS2-1.0")

    with pytest.raises(LiveMergeApprovalError, match="scope"):
        load_and_validate_approval(
            path,
            expected_scope=_EXPECTED_SCOPE,
            expected_target_graph=_EXPECTED_TARGET_GRAPH,
            run_kind="initial",
        )


def test_wrong_target_graph_raises_error(tmp_path: Path) -> None:
    """A disposable graph's own approval must never satisfy the real-graph gate."""
    path = _write_approval_file(tmp_path, target_graph="policy_system_capstone_test")

    with pytest.raises(LiveMergeApprovalError, match="target_graph"):
        load_and_validate_approval(
            path,
            expected_scope=_EXPECTED_SCOPE,
            expected_target_graph=_EXPECTED_TARGET_GRAPH,
            run_kind="initial",
        )


def test_confirmation_phrase_off_by_one_character_raises_error(tmp_path: Path) -> None:
    path = _write_approval_file(
        tmp_path, confirmation_phrase="I APPROVE THE LIVE MERGE INTO policy_System"
    )

    with pytest.raises(LiveMergeApprovalError, match="confirmation_phrase"):
        load_and_validate_approval(
            path,
            expected_scope=_EXPECTED_SCOPE,
            expected_target_graph=_EXPECTED_TARGET_GRAPH,
            run_kind="initial",
        )


def test_initial_phrase_rejected_when_validated_as_rerun(tmp_path: Path) -> None:
    """The two run_kind phrases are distinct and non-interchangeable."""
    path = _write_approval_file(tmp_path, confirmation_phrase=_INITIAL_PHRASE)

    with pytest.raises(LiveMergeApprovalError, match="confirmation_phrase"):
        load_and_validate_approval(
            path,
            expected_scope=_EXPECTED_SCOPE,
            expected_target_graph=_EXPECTED_TARGET_GRAPH,
            run_kind="rerun",
        )
