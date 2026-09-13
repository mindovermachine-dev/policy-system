"""Approval-gate mechanism for a live write into the real `policy_system` graph.

AC-BI-001 (issue #28): a live merge into the real, shared `policy_system`
graph must not proceed without explicit, recorded human approval. This
module never writes an approval file -- it only ever reads and validates
one a human authored by hand elsewhere.

Per issue #28's tracker CHANGES.md row #3: the two confirmation phrases are
hardcoded module constants (`_CONFIRMATION_PHRASE_BY_RUN_KIND`), selected by
`run_kind`, never supplied by a caller as a string. A caller-supplied
"expected phrase" argument would let whoever runs the tool pass their own
phrase via argv and have it match whatever they themselves wrote into the
approval file -- a self-approval loophole. The mechanism instead forces a
human to know and correctly type an out-of-band phrase (documented in this
issue's own CONTEXT.md/PLAN.md, never printed by this module) into a file
this module's own code never writes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from pathlib import Path

_CONFIRMATION_PHRASE_BY_RUN_KIND: dict[str, str] = {
    "initial": "I APPROVE THE LIVE MERGE INTO policy_system",
    "rerun": "I APPROVE THE LIVE MERGE RE-RUN INTO policy_system",
}

_REQUIRED_FIELDS: tuple[str, ...] = (
    "approved_by",
    "approved_at",
    "scope",
    "target_graph",
    "precondition_check_result",
    "confirmation_phrase",
)


class LiveMergeApprovalError(Exception):
    """An approval record is missing, malformed, or does not authorize this run.

    Raised by `load_and_validate_approval`, naming exactly what is wrong --
    a missing file, a missing/blank required field, a `scope` or
    `target_graph` mismatch, or a `confirmation_phrase` that does not
    exactly match the phrase hardcoded for the given `run_kind` -- before
    any live write is attempted.
    """


@dataclass(frozen=True, slots=True)
class LiveMergeApproval:
    """A parsed, validated human approval record for one live-merge invocation."""

    approved_by: str
    approved_at: str
    scope: tuple[str, ...]
    target_graph: str
    precondition_check_result: str
    confirmation_phrase: str


def _parse_fields(path: Path) -> dict[str, str]:
    """Parse the file's plain `key: value` lines -- no YAML dependency needed.

    Only lines whose key matches one of `_REQUIRED_FIELDS` are kept; every
    other line (blank lines, headings, commentary) is ignored. A value may
    itself contain colons (e.g. an ISO timestamp, or pasted precondition
    output) since only the *first* colon on a line separates key from value.
    """
    fields: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        if key in _REQUIRED_FIELDS:
            fields[key] = value.strip()
    return fields


def load_and_validate_approval(
    path: Path,
    *,
    expected_scope: tuple[str, ...],
    expected_target_graph: str,
    run_kind: Literal["initial", "rerun"],
) -> LiveMergeApproval:
    """Parse and validate an approval record. Never writes anything.

    Raises `LiveMergeApprovalError`, naming exactly what is wrong, when:
    the file at `path` does not exist; a required field is missing or
    blank; `scope` does not equal `expected_scope` as a set; `target_graph`
    does not equal `expected_target_graph`; or `confirmation_phrase` does
    not exactly match the phrase hardcoded for `run_kind` in
    `_CONFIRMATION_PHRASE_BY_RUN_KIND` (an exact match, never fuzzy -- a
    human must have typed or copied the literal phrase deliberately; a
    near-miss, e.g. a single changed character, fails closed).
    """
    if not path.is_file():
        missing_file_msg = f"Approval file not found: {path}"
        raise LiveMergeApprovalError(missing_file_msg)

    fields = _parse_fields(path)
    for field_name in _REQUIRED_FIELDS:
        if not fields.get(field_name):
            missing_field_msg = f"Approval file {path} is missing required field: {field_name}"
            raise LiveMergeApprovalError(missing_field_msg)

    scope = tuple(item.strip() for item in fields["scope"].split(",") if item.strip())
    if set(scope) != set(expected_scope):
        scope_mismatch_msg = (
            f"Approval file {path} scope {scope} does not match expected scope {expected_scope}"
        )
        raise LiveMergeApprovalError(scope_mismatch_msg)

    target_graph = fields["target_graph"]
    if target_graph != expected_target_graph:
        target_graph_mismatch_msg = (
            f"Approval file {path} target_graph '{target_graph}' does not match "
            f"expected target_graph '{expected_target_graph}'"
        )
        raise LiveMergeApprovalError(target_graph_mismatch_msg)

    expected_phrase = _CONFIRMATION_PHRASE_BY_RUN_KIND[run_kind]
    if fields["confirmation_phrase"] != expected_phrase:
        phrase_mismatch_msg = (
            f"Approval file {path} confirmation_phrase does not exactly match "
            f"the required phrase for run_kind '{run_kind}'"
        )
        raise LiveMergeApprovalError(phrase_mismatch_msg)

    return LiveMergeApproval(
        approved_by=fields["approved_by"],
        approved_at=fields["approved_at"],
        scope=scope,
        target_graph=target_graph,
        precondition_check_result=fields["precondition_check_result"],
        confirmation_phrase=fields["confirmation_phrase"],
    )
