"""Content lint for `ps-verify-audit-log/SKILL.md`.

Pins that the skill documents the `verify-audit-log` tool, its role gate, every named error
state, the result shape (an intact report, and a break as kind, sequence, id and time rather
than a bare pass or fail), the tamper-evidence limit, and the no-fabrication guardrails, and that
the plugin listing and the neighbouring audit skill point at it.
"""

from __future__ import annotations

from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3] / "ps-skills"
_SKILL = _ROOT / "ps-plugin" / "skills" / "ps-verify-audit-log" / "SKILL.md"


def _text(path: Path) -> str:
    return " ".join(path.read_text(encoding="utf-8").split())


def test_the_skill_exists_with_its_name_in_the_frontmatter() -> None:
    assert _SKILL.is_file()
    assert _SKILL.read_text(encoding="utf-8").startswith("---\nname: ps-verify-audit-log\n")


def test_the_skill_documents_the_tool_the_connector_and_the_role_gate() -> None:
    text = _text(_SKILL)

    assert "verify-audit-log" in text
    assert "ps-mcp" in text
    assert "SystemOwner" in text
    assert "SystemAdmin" in text
    assert "read-only" in text.lower()


def test_the_skill_names_every_error_state_of_the_tool() -> None:
    text = _text(_SKILL)

    assert "error: access-role management requires a real authenticated caller" in text
    assert "error: You do not have the required access role for this action." in text
    assert "access_denied" in text
    assert "error: The authorization store is temporarily unavailable." in text
    assert "authorization_store_unavailable" in text
    assert "error: an unexpected error occurred" in text
    assert "local-test bypass" in text


def test_the_skill_reports_an_intact_chain_with_its_counts() -> None:
    text = _text(_SKILL)

    assert "intact" in text
    assert "rows_checked" in text
    assert "head_chain_seq" in text


def test_the_skill_reports_a_break_as_kind_sequence_id_and_time_never_a_bare_verdict() -> None:
    text = _text(_SKILL)

    for kind in ("sequence_gap", "prev_hash_mismatch", "row_hash_mismatch", "head_mismatch"):
        assert kind in text
    for field in ("first_break", "chain_seq", "occurred_at"):
        assert field in text
    assert "never" in text.lower()
    assert "bare pass or fail" in text


def test_the_skill_states_the_tamper_evidence_limit() -> None:
    text = _text(_SKILL).lower()

    assert "tamper-evident" in text
    assert "superuser" in text
    assert "database owner" in text


def test_the_skill_forbids_fabrication_and_direct_postgres_access() -> None:
    text = _text(_SKILL)

    assert "Never fabricate" in text
    assert "never a direct Postgres connection" in text


def test_the_plugin_readme_lists_the_skill() -> None:
    assert "ps-verify-audit-log" in _text(_ROOT / "readme.md")


def test_the_list_audit_events_skill_points_to_the_verification_skill() -> None:
    text = _text(_ROOT / "ps-plugin" / "skills" / "ps-list-audit-events" / "SKILL.md")

    assert "ps-verify-audit-log" in text
