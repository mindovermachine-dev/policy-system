"""Content lint for `ps-ingest-regulation/SKILL.md` and its docs (issue #193).

Pins that the skill, the Container Architecture row and the user guide describe
the live-graph identity check (no curated-catalog cross-check) consistently.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[3]
_SKILL = _REPO / "ps-skills" / "ps-plugin" / "skills" / "ps-ingest-regulation" / "SKILL.md"
_ARCH = _REPO / "docs" / "architecture" / "ps-service-container-architecture.md"
_USER_GUIDE = _REPO / "docs" / "artifacts" / "user-guide.md"


_SKILL_DIR = _SKILL.parent
_REFERENCES = ("references/blocking-fallback.md", "references/error-states.md")


def _flat(path: Path) -> str:
    return " ".join(path.read_text(encoding="utf-8").split())


def _flat_skill_with_references() -> str:
    """SKILL.md plus its on-demand reference files (issue #200), whitespace-flattened."""
    parts = [_SKILL.read_text(encoding="utf-8")]
    parts += [(_SKILL_DIR / ref).read_text(encoding="utf-8") for ref in _REFERENCES]
    return " ".join("\n".join(parts).split())


def _arch_ingest_regulation_row() -> str:
    for line in _ARCH.read_text(encoding="utf-8").splitlines():
        if line.startswith("| IngestRegulation |"):
            return " ".join(line.split())
    pytest.fail("IngestRegulation row not found in the Container Architecture")


def test_skill_has_no_curated_mismatch_state() -> None:
    text = _flat_skill_with_references()

    assert "is curated under short_name" not in text
    assert "curated mismatch" not in text.lower()
    assert "curated-catalog cross-check" not in text


def test_skill_reports_the_already_ingested_celex_error() -> None:
    text = _flat_skill_with_references()

    assert "error: CELEX <celex> is already ingested as short_name '<existing>'" in text
    assert "error: short_name '<given>' is already claimed by CELEX <other celex>" in text


def test_skill_not_found_message_mentions_only_cellar() -> None:
    text = _flat_skill_with_references()

    assert "error: CELEX '<celex>' does not exist on Cellar/ELI." in text
    assert "No curated regulation has CELEX" not in text


def test_skill_describes_already_ingested_outcome_as_legacy_only() -> None:
    text = _flat_skill_with_references()

    assert "legacy" in text.lower()
    assert "celex-less" in text.lower()


def test_skill_states_short_name_is_normalised_to_upper_case() -> None:
    assert "upper case" in _flat_skill_with_references().lower()


def test_architecture_row_describes_live_graph_identity_check() -> None:
    row = _arch_ingest_regulation_row()

    assert "validate_and_resolve_catalog_entry" not in row
    assert "curated/`short_name` mismatch" not in row
    assert "celex_already_ingested" in row
    assert "case-insensitively" in row
    assert "upper case" in row


def test_user_guide_states_normalisation_and_existing_celex_rejection() -> None:
    text = _flat(_USER_GUIDE)

    assert "upper case" in text
    assert "already ingested" in text


def test_issue_200_on_demand_references_exist_and_are_named_with_their_condition() -> None:
    text = _flat(_SKILL)

    for reference in _REFERENCES:
        assert (_SKILL_DIR / reference).is_file(), f"{reference} is missing"
        assert f"read `{reference}`" in text, f"SKILL.md never says when to read {reference}"
        body = (_SKILL_DIR / reference).read_text(encoding="utf-8")
        assert "Every Guardrail in `SKILL.md` still applies" in body
