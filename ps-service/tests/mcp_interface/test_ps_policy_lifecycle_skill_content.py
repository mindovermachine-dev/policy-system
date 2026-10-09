"""Content lint for `ps-policy-lifecycle/SKILL.md` (issue #185, S9).

Pins that the skill documents `capability_ids` on `create-policy-draft`, the
`GOVERNED_BY` move on `approve-policy`, and the exact text of the three new
error messages built from `ps_service.policy_lifecycle.errors`.
"""

from __future__ import annotations

from pathlib import Path

from ps_service.policy_lifecycle.errors import (
    PolicyCapabilityAlreadyGovernedError,
    PolicyCapabilityNotFoundError,
    PolicyGovernanceConflictError,
)

_SKILL_DIR = (
    Path(__file__).resolve().parents[3]
    / "ps-skills"
    / "ps-plugin"
    / "skills"
    / "ps-policy-lifecycle"
)
_SKILL_PATH = _SKILL_DIR / "SKILL.md"
# Issue #200: rarely-needed detail is read on demand from these files.
_REFERENCES: tuple[str, ...] = (
    "references/transition-rules.md",
    "references/local-test-bypass.md",
    "references/error-states.md",
    "references/transition-output.md",
)


def _skill_text() -> str:
    # SKILL.md plus its on-demand references: the documented content is the union.
    parts = [_SKILL_PATH.read_text(encoding="utf-8")]
    parts += [(_SKILL_DIR / ref).read_text(encoding="utf-8") for ref in _REFERENCES]
    return " ".join("\n".join(parts).split())


def test_documents_capability_ids_and_governed_capability_ids() -> None:
    text = _skill_text()

    assert "capability_ids" in text
    assert "governed_capability_ids" in text
    assert "GOVERNED_BY" in text


def test_no_longer_calls_the_supersede_fork_unbuilt() -> None:
    assert "not-yet-built" not in _skill_text()


def test_new_error_messages_match_errors_py_text() -> None:
    text = _skill_text()
    samples = (
        str(PolicyCapabilityNotFoundError(("<capability_id>",))),
        str(PolicyCapabilityAlreadyGovernedError(("<capability_id>",))),
        str(PolicyGovernanceConflictError("<policy_id>")),
    )

    for message in samples:
        assert f"error: {message}" in text, message


def test_issue_200_on_demand_references_exist_and_are_named_with_their_condition() -> None:
    text = " ".join(_SKILL_PATH.read_text(encoding="utf-8").split())

    for reference in _REFERENCES:
        path = _SKILL_DIR / reference
        assert path.is_file(), f"{reference} is missing"
        assert f"read `{reference}`" in text, f"SKILL.md never says when to read {reference}"
        assert "Guardrail" in path.read_text(encoding="utf-8"), reference
