"""Documentation lint for the role gate and the audit rows it added.

A whole-tree docs lint, a deliberate exception to the one-to-one `tests/` to `src/` mirror: it
reads source, tests, the container architecture, the audit reports, the user guide and the skill
files. It pins that superseded statements are gone and that the new actions, the gate and the
audit-report rows are documented.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from ps_service.audit import emit
from ps_service.mcp_interface import (
    access_audit_tools,
    cypher_tools,
    near_miss_tools,
    policy_lifecycle_tools,
)

if TYPE_CHECKING:
    from collections.abc import Callable

_REPO = Path(__file__).resolve().parents[3]
_SRC = _REPO / "ps-service" / "src" / "ps_service"
_TESTS = _REPO / "ps-service" / "tests"
_ARCHITECTURE = _REPO / "docs" / "architecture" / "ps-service-container-architecture.md"
_LOGGING_AUDIT = _REPO / "docs" / "audits" / "logging-audit.md"
_DOCUMENTATION_AUDIT = _REPO / "docs" / "audits" / "documentation-audit.md"
_USER_GUIDE = _REPO / "docs" / "artifacts" / "user-guide.md"
_NEW_ACTIONS = ("access.gate", "passkey.enroll", "query.cypher")

# The removed function's name, assembled so this file does not match its own scan.
_REMOVED_NAMES = re.compile(r"\b(?:require" + r"_role|Require" + r"Role)\b")
_SUPERSEDED_PHRASES = (
    re.compile(r"No audit event is written for a denial"),
    re.compile(r"role[- ]check[^.\n]{0,80}records no audit event", re.IGNORECASE),
    re.compile(r"records no audit event[^.\n]{0,80}role[- ]check", re.IGNORECASE),
)
# Files that name the removed function on purpose, to assert it is gone.
_ALLOWED_STALE_NAME_FILES = frozenset(
    {
        Path(__file__).resolve(),
        _TESTS / "authz" / "test_role_gate.py",  # `test_require_role_is_no_longer_exported`
        _TESTS / "mcp_interface" / "test_gate_declarations.py",  # the hand-written-gate AST scan
    }
)


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _section(text: str, start: str, end: str) -> str:
    begin = text.index(start)
    return text[begin : text.index(end, begin)]


def _files(root: Path, suffixes: tuple[str, ...]) -> list[Path]:
    return sorted(
        path
        for path in root.rglob("*")
        if path.suffix in suffixes and "__pycache__" not in path.parts and path.is_file()
    )


def test_no_source_text_says_a_role_denial_writes_no_audit_event() -> None:
    offenders = [
        f"{path.relative_to(_REPO)}: {pattern.pattern}"
        for path in _files(_SRC, (".py",))
        for pattern in _SUPERSEDED_PHRASES
        if pattern.search(_text(path))
    ]

    assert offenders == []


def test_no_stale_require_role_reference_remains() -> None:
    scanned = (
        _files(_SRC, (".py",))
        + _files(_TESTS, (".py",))
        + _files(_REPO / "docs" / "architecture", (".md",))
        + _files(_REPO / "ps-skills", (".md",))
    )
    offenders = [
        str(path.relative_to(_REPO))
        for path in scanned
        if path.resolve() not in _ALLOWED_STALE_NAME_FILES and _REMOVED_NAMES.search(_text(path))
    ]

    assert offenders == []


def test_emit_conventions_list_the_new_actions() -> None:
    doc = emit.__doc__ or ""

    for action in _NEW_ACTIONS:
        assert action in doc, action


def test_list_audit_events_tool_docstring_lists_the_new_actions() -> None:
    doc = access_audit_tools.list_audit_events.__doc__ or ""

    for action in _NEW_ACTIONS:
        assert action in doc, action


@pytest.mark.parametrize(
    "tool",
    [
        near_miss_tools.near_misses_resolve,
        policy_lifecycle_tools.approve_policy,
        policy_lifecycle_tools.reject_policy,
    ],
)
def test_body_gated_tool_docstrings_describe_the_access_gate_denial_row(
    tool: Callable[..., object],
) -> None:
    doc = " ".join((tool.__doc__ or "").split())

    assert "`access.gate` audit row" in doc


def test_cypher_tool_docstring_says_it_writes_one_audit_row_and_nothing_to_the_graph() -> None:
    doc = " ".join((cypher_tools.cypher.__doc__ or "").split())

    assert "query.cypher" in doc
    assert "nothing is written to the graph" in doc


def test_architecture_audit_section_lists_the_new_actions_and_the_attributable_facts_rule() -> None:
    section = _section(_text(_ARCHITECTURE), "\n### Audit\n", "\n### Graph Write Gateway\n")

    for action in _NEW_ACTIONS:
        assert action in section, action
    assert "attributable" in section.lower()
    assert "best-effort" in section.lower()


@pytest.mark.parametrize(
    "path",
    [
        "ps-service/src/ps_service/mcp_interface/tool_gates.py",
        "ps-service/src/ps_service/mcp_interface/audit_actions.py",
        "ps-service/src/ps_service/authz/request_gate.py",
        "ps-service/src/ps_service/passkey_signing/audit_actions.py",
    ],
)
def test_architecture_file_tables_list_every_new_module(path: str) -> None:
    assert f"`{path}`" in _text(_ARCHITECTURE)


def test_architecture_authorization_names_the_pure_check_and_the_request_gate() -> None:
    section = _section(_text(_ARCHITECTURE), "\n### Authorization\n", "\n### Policy Lifecycle\n")

    assert "CheckRole" in section
    assert "EnforceRoleGate" in section
    assert "`check_role`" in section
    assert "`enforce_role_gate`" in section


def test_logging_audit_marks_log_12_fixed_for_these_gaps_and_open_for_the_rest() -> None:
    (row,) = [line for line in _text(_LOGGING_AUDIT).splitlines() if line.startswith("| LOG-12 ")]

    assert "fixed in this change" in row
    for covered in ("role-gate denials", "passkey enrolment", "`cypher`"):
        assert covered in row, covered
    assert "still open" in row
    for remaining in ("failed authentication", "passkey rejections", "retention"):
        assert remaining in row, remaining


def test_logging_audit_enforcement_rows_split_the_covered_actions_from_the_rest() -> None:
    rows = [
        line
        for line in _text(_LOGGING_AUDIT).splitlines()
        if line.startswith("| Audit records for")
    ]
    covered = [row for row in rows if "| enforced |" in row]
    remaining = [row for row in rows if "| review-only |" in row]

    assert len(covered) == 1
    for name in ("role-gate denials", "passkey enrolment", "`cypher`"):
        assert name in covered[0], name
    assert "gate-declaration" in covered[0]
    assert len(remaining) == 1


def test_logging_audit_canary_section_counts_the_tests_in_the_canary_file() -> None:
    words = {6: "six", 7: "seven", 8: "eight", 9: "nine", 10: "ten", 11: "eleven", 12: "twelve"}
    canary = _TESTS / "logging" / "test_log_leakage_canary.py"
    count = len(re.findall(r"^def test_", _text(canary), flags=re.MULTILINE))

    assert f"holds {words[count]} tests" in _text(_LOGGING_AUDIT)


def test_documentation_audit_describes_the_current_authorization_mechanism() -> None:
    text = _text(_DOCUMENTATION_AUDIT)
    line = next(ln for ln in text.splitlines() if ln.startswith("- MCP authorization:"))

    assert _REMOVED_NAMES.search(line) is None
    assert "check_role" in line
    assert "tool_gates.py" in line


def test_user_guide_lists_the_new_audit_rows_and_what_is_not_recorded() -> None:
    section = _section(
        _text(_USER_GUIDE), "### Review the audit trail", "If the audit trail cannot record"
    )

    for action in _NEW_ACTIONS:
        assert action in section, action
    assert "passkey rejections" in section.lower()
