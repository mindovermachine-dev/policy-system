# © 2026 Cartman ApS. All rights reserved.
"""Stage 1 -- term-coverage check (README.md, "Stage 1").

Deliberately thin coverage, not thin realness: the curated cluster below
covers exactly the terms the known target cases turn on (see
PROGRESS.md's "Target cases" table), pulled verbatim from
`.github/skills/ps-domain/SKILL.md`'s Canonical Definitions section. This
is NOT a generic thesaurus -- growing it is a deliberate, reviewed act
(README.md), the same discipline as the Known-Gaps Registry.

A "no match" on a term outside this table is the correct, intended output
for a genuinely novel term -- it is not this module's job to guess.
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional

from .types import MatchKind, Stage1Result, TermMatch

# --------------------------------------------------------------------------
# The curated alias table.
#
# `disambiguation_required=True` marks a small cluster of status terms that
# sound like near-synonyms but are deliberately distinct per SKILL.md --
# Stage 4's fitness gate must confirm the answer applied *this* definition,
# not a looser reading. Growing this cluster (or the table generally) is
# reviewed work, same discipline as the Known-Gaps Registry -- see
# README.md's Stage 1 section.
# --------------------------------------------------------------------------

ALIAS_TABLE: Dict[str, dict] = {
    "overdue": {
        "aliases": [],
        "definition": (
            "A Control's next_review_date has passed. Deprecated Controls "
            "are excluded -- a deprecated Control has left the review "
            "cycle, it has not failed it."
        ),
        "disambiguation_required": True,
    },
    "stale": {
        "aliases": [],
        "definition": (
            "The chain from Capability to a current Control is broken (no "
            "IMPLEMENTED_BY Control in implemented or reviewed status) -- "
            "not merely an overdue review on an otherwise-live chain. A "
            "live Control with a lapsed review is 'overdue,' not 'stale.' "
            "Do not conflate the two when counting."
        ),
        "disambiguation_required": True,
    },
    "deprecated": {
        "aliases": [],
        "definition": (
            "Lifecycle status on a Policy/Standard/Control indicating it "
            "has left the review cycle -- not the same claim as having "
            "failed that cycle (see 'overdue')."
        ),
        "disambiguation_required": True,
    },
}

# Entity-type nouns Stage 4's entity-type cross-check needs recorded from
# the question text (README.md: "record which canonical entity-type it
# asks about, e.g. 'chain' vs 'control'"). Order matters only for the
# earliest-position tie-break in extract_entity_type below, not for
# correctness -- every pattern is checked regardless of position.
_ENTITY_TYPE_PATTERNS: List[tuple] = [
    (r"\bchains?\b", "chain"),
    (r"\b(controls?|checks?)\b", "control"),
    (r"\bcapabilit(?:y|ies)\b", "capability"),
    (r"\bobligations?\b|\bduties\b|\bduty\b", "obligation"),
    (r"\bpolic(?:y|ies)\b", "policy"),
    (r"\bstandards?\b", "standard"),
    (r"\bregulations?\b", "regulation"),
    (r"\brequirements?\b", "requirement"),
]


def check_terms(question_text: str) -> List[TermMatch]:
    """Match every ALIAS_TABLE term (exact or curated alias) found in the
    question text. A term not found at all is simply absent from the
    result -- this function only returns matches, never NO_MATCH entries;
    NO_MATCH is for a caller-supplied vocabulary list Stage 1 doesn't own
    here (this table only tracks confusable status terms, not the full
    domain vocabulary -- see PROGRESS.md's open scope note).
    """
    text_lower = question_text.lower()
    matches: List[TermMatch] = []
    for canonical, spec in ALIAS_TABLE.items():
        if re.search(r"\b" + re.escape(canonical) + r"\b", text_lower):
            matches.append(
                TermMatch(
                    surface_text=canonical,
                    canonical_term=canonical,
                    kind=MatchKind.EXACT,
                    definition=spec["definition"],
                    disambiguation_required=spec["disambiguation_required"],
                )
            )
            continue
        for alias in spec["aliases"]:
            if re.search(r"\b" + re.escape(alias) + r"\b", text_lower):
                matches.append(
                    TermMatch(
                        surface_text=alias,
                        canonical_term=canonical,
                        kind=MatchKind.ALIAS,
                        definition=spec["definition"],
                        disambiguation_required=spec["disambiguation_required"],
                    )
                )
                break
    return matches


def extract_entity_type(question_text: str) -> Optional[str]:
    """Return the canonical entity-type noun that appears earliest in the
    question text, or None if none of the tracked nouns appear. Earliest-
    position is a deliberate, simple heuristic (not NLP) -- validated
    against EM-E3 ("how many of our ... chains") where it is unambiguous;
    see PROGRESS.md for cases where this heuristic is known to be too
    weak to rely on (EM-M4).
    """
    best: Optional[tuple] = None  # (start_index, canonical)
    for pattern, canonical in _ENTITY_TYPE_PATTERNS:
        m = re.search(pattern, question_text, re.IGNORECASE)
        if m and (best is None or m.start() < best[0]):
            best = (m.start(), canonical)
    return best[1] if best else None


def run_stage1(question_id: str, question_text: str) -> Stage1Result:
    return Stage1Result(
        question_id=question_id,
        question_text=question_text,
        term_matches=check_terms(question_text),
        entity_type=extract_entity_type(question_text),
    )
