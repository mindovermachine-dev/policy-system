"""Content lint for `ps-list-audit-events/SKILL.md` (issue #195).

Pins that the skill documents the allow-listed `details` filter, the audited actions added by
#195 and the unknown-key `error:` state, and that the audit-adjacent skills tell the user what is
recorded.
"""

from __future__ import annotations

from pathlib import Path

from ps_service.audit import AUDIT_DETAILS_FILTER_KEYS

_SKILLS = Path(__file__).resolve().parents[3] / "ps-skills" / "ps-plugin" / "skills"


def _text(skill: str) -> str:
    return " ".join((_SKILLS / skill / "SKILL.md").read_text(encoding="utf-8").split())


def test_list_audit_events_skill_documents_the_details_filter_and_new_actions() -> None:
    text = _text("ps-list-audit-events")

    assert "details" in text
    for key in AUDIT_DETAILS_FILTER_KEYS:
        assert key in text
    for action in (
        "ingestion_run.submit",
        "ingestion_run.complete",
        "instrument.restore",
        "near_miss.resolve",
        "user.invite",
    ):
        assert action in text
    assert "who ingested" in text.lower()


def test_list_audit_events_skill_maps_the_unknown_details_key_error_to_a_named_state() -> None:
    text = _text("ps-list-audit-events")

    assert "invalid_details_filter" in text
    assert "allow-list" in text.lower()


def test_check_regulations_skill_says_each_reingest_is_audited_with_the_callers_identity() -> None:
    text = _text("ps-check-regulations").lower()

    assert "amendment_check" in text
    assert "audit" in text
    assert "list-audit-events" in text


def test_ingest_and_restore_skills_point_to_the_audit_trail() -> None:
    for skill in ("ps-ingest-regulation", "ps-restore-instrument"):
        text = _text(skill).lower()
        assert "audit" in text, skill
        assert "list-audit-events" in text, skill
