"""Content lint for `ps-assess-instrument-applicability/SKILL.md` (issue #200).

Reads the real `SKILL.md` off disk and asserts that the progressive-disclosure
split kept the skill intact:

- Frontmatter has `name` and a non-empty `description`.
- The two example interactions live in `references/` and `SKILL.md` names each
  with its `read ...` condition.
- The Guardrails section still carries its key safety statements word for word.
- On Load makes no `domain_concepts` fetch.
"""

from __future__ import annotations

from pathlib import Path
from typing import cast

import yaml

_SKILL_DIR = (
    Path(__file__).resolve().parents[3]
    / "ps-skills"
    / "ps-plugin"
    / "skills"
    / "ps-assess-instrument-applicability"
)
_SKILL_PATH = _SKILL_DIR / "SKILL.md"
_REFERENCES: tuple[str, ...] = (
    "references/example-interaction.md",
    "references/example-degraded-path.md",
)

_GUARDRAIL_SENTENCES: tuple[str, ...] = (
    (
        "Never generate any part of the candidate list before the Compliance Officer has "
        "explicitly confirmed the captured markets/geographies and products/services."
    ),
    "Never fabricate a CELEX identifier for any candidate",
    "Never omit an excluded candidate, or leave a likely/possible candidate's tier unexplained.",
    (
        "Never abort the assessment because `check_instrument_ingestion_status` could not be "
        "reached or could not resolve a candidate"
    ),
)


def _normalise(text: str) -> str:
    return " ".join(text.split())


def _split_frontmatter(text: str) -> tuple[dict[str, object], str]:
    assert text.startswith("---\n"), "SKILL.md must open with a `---` frontmatter fence"
    _, _, rest = text.partition("---\n")
    frontmatter_text, fence, body = rest.partition("\n---\n")
    assert fence, "SKILL.md's frontmatter fence was never closed"
    loaded = yaml.safe_load(frontmatter_text)
    assert isinstance(loaded, dict)
    return cast("dict[str, object]", loaded), body


def _section_text(body: str, heading: str) -> str:
    lines = body.splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip() == heading)
    end = len(lines)
    for i in range(start + 1, len(lines)):
        if lines[i].startswith("## "):
            end = i
            break
    return "\n".join(lines[start:end])


def test_frontmatter_has_name_and_description() -> None:
    frontmatter, _body = _split_frontmatter(_SKILL_PATH.read_text(encoding="utf-8"))

    assert frontmatter["name"] == "ps-assess-instrument-applicability"
    assert isinstance(frontmatter["description"], str)
    assert frontmatter["description"].strip() != ""


def test_issue_200_on_demand_references_exist_and_are_named_with_their_condition() -> None:
    text = _normalise(_SKILL_PATH.read_text(encoding="utf-8"))

    for reference in _REFERENCES:
        path = _SKILL_DIR / reference
        assert path.is_file(), f"{reference} is missing"
        assert "Guardrail in `SKILL.md` still applies" in path.read_text(encoding="utf-8")
        assert f"read `{reference}`" in text, f"SKILL.md never says when to read {reference}"


def test_guardrails_keep_their_key_safety_statements() -> None:
    _frontmatter, body = _split_frontmatter(_SKILL_PATH.read_text(encoding="utf-8"))
    guardrails = _normalise(_section_text(body, "## Guardrails"))

    for sentence in _GUARDRAIL_SENTENCES:
        assert sentence in guardrails, f"Guardrails lost: {sentence!r}"


def test_issue_200_on_load_makes_no_domain_concepts_fetch() -> None:
    _frontmatter, body = _split_frontmatter(_SKILL_PATH.read_text(encoding="utf-8"))

    assert "domain_concepts" not in _section_text(body, "## On Load")
