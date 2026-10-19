"""Documentation lint for the rule that log entries carry no user identifier.

A whole-tree docs lint, a deliberate exception to the one-to-one `tests/` to `src/` mirror: it
reads the container architecture, the Python coding standard and the logging audit and pins that
their statements about what each log entry carries match the code. Matched phrases are assembled
by concatenation so this file does not match its own scan.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[3]
_ARCHITECTURE = _REPO / "docs" / "architecture" / "ps-service-container-architecture.md"
_PYTHON_STANDARD = _REPO / "docs" / "coding-standards" / "level2-python-instructions.md"
_LOGGING_AUDIT = _REPO / "docs" / "audits" / "logging-audit.md"


def _architecture_row(action: str) -> str:
    """Return the container-architecture table row for `action`."""
    prefix = f"| {action} |"
    rows = [
        line
        for line in _ARCHITECTURE.read_text(encoding="utf-8").splitlines()
        if line.startswith(prefix)
    ]
    assert len(rows) == 1, f"expected one {action} row, found {len(rows)}"
    return rows[0]


def test_container_architecture_verify_token_row_logs_no_token_claim_on_success() -> None:
    row = _architecture_row("VerifyToken")

    assert "`sub`" + "+" + "`iss` on success" not in row
    assert "`outcome=success` with no token claim" in row


def test_architecture_logging_section_attaches_no_local_test_principal_to_query_entries() -> None:
    text = _ARCHITECTURE.read_text(encoding="utf-8")

    assert "attached to every query" + "'s structured log entry" not in text


def test_container_architecture_logging_section_says_log_entries_carry_no_user_identifier() -> None:
    text = _ARCHITECTURE.read_text(encoding="utf-8")
    logging_section = text.split("\n### Logging\n", 1)[1].split("\n### ", 1)[0]

    assert (
        "Log entries carry no user identifier; actor attribution is in the Audit component."
        in logging_section
    )


def test_python_standard_target_state_sentence_no_longer_lists_user_identifiers() -> None:
    text = _PYTHON_STANDARD.read_text(encoding="utf-8")
    [target_state] = [line for line in text.splitlines() if "Target state, not current" in line]

    assert "user " + "identifiers" not in target_state
    assert target_state == "- Target state, not current behaviour: a closed outcome vocabulary."


def test_container_architecture_restore_row_names_the_mcp_caller_label_as_the_restore_actor() -> (
    None
):
    row = _architecture_row("RestoreInstrumentFromCatalog")

    assert "`actor` argument is set to that same " + "resolved principal" not in row
    assert "`actor` argument is the fixed MCP caller label `mcp`" in row


_TRIAD_TOOL_ROWS = (
    "IngestRegulation",
    "CheckRegulations",
    "ListNearMisses",
    "ResolveNearMiss",
    "GetCatalogListing",
    "RestoreInstrumentFromCatalog",
)


@pytest.mark.parametrize("action", _TRIAD_TOOL_ROWS)
def test_container_architecture_tool_rows_no_longer_say_the_log_triad_carries_the_principal(
    action: str,
) -> None:
    row = _architecture_row(action)

    assert "carrying the resolved " + "principal" not in row
    assert "actor-identified " + "log lines" not in row
    assert (
        "log triad correlated by one `run_id`; the caller is attributed in the audit trail" in row
    )


@pytest.mark.parametrize("action", _TRIAD_TOOL_ROWS)
def test_container_architecture_tool_rows_close_the_audit_trail_clause_with_punctuation(
    action: str,
) -> None:
    row = _architecture_row(action)

    assert re.search(r"not the log[.,]", row), f"{action}: clause runs into the next sentence"


def _logging_audit_rows(prefix: str) -> list[str]:
    """Return the logging-audit table rows that start with `prefix`."""
    lines = _LOGGING_AUDIT.read_text(encoding="utf-8").splitlines()
    return [line for line in lines if line.startswith(prefix)]


def test_logging_audit_shows_log07_fixed_and_identifier_rule_enforced() -> None:
    text = _LOGGING_AUDIT.read_text(encoding="utf-8")
    [log07] = _logging_audit_rows("| LOG-07 |")

    assert log07.split("|")[-2].strip().startswith("fixed")
    assert "Partially " + "met" not in text
    assert _logging_audit_rows("| User identifiers do not appear in log entries |") == [
        (
            "| User identifiers do not appear in log entries | enforced | "
            "`pytest` (log-leakage canary test) |"
        )
    ]
    [exception_text] = _logging_audit_rows("| Exception text in log entries |")
    assert exception_text.split("|")[2].strip() == "review-only"
    assert _logging_audit_rows("| Exception text and user " + "identifiers") == []


@pytest.mark.parametrize(
    ("flow", "canaries"),
    [
        ("| Accepted bearer token with a canary subject |", "raw token, `Bearer ` prefix, user"),
        ("| MCP tool calls by a canary principal (`cypher`, gate refusal) |", "user"),
        ("| Invite flow by a canary caller for a canary email |", "user, email"),
    ],
)
def test_logging_audit_canary_table_lists_the_identifier_flows(flow: str, canaries: str) -> None:
    assert _logging_audit_rows(flow) == [f"{flow} {canaries} |"]
