# © 2026 Cartman ApS. All rights reserved.
"""Shared data shapes for the pipeline stages.

Kept deliberately small -- these are what Stage 1/2 produce and what
Stage 4 consumes, not a general-purpose framework. Extend only when a
concrete stage needs a new field, not speculatively.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional


class MatchKind(Enum):
    EXACT = "exact"
    ALIAS = "alias"
    NO_MATCH = "no_match"


@dataclass
class TermMatch:
    """One vocabulary term found in a question, and how it resolved
    against the alias table (README.md Stage 1)."""

    surface_text: str
    canonical_term: Optional[str]
    kind: MatchKind
    definition: Optional[str] = None
    # True for a small curated cluster of terms that sound like synonyms
    # but are deliberately distinct (stale/overdue/deprecated) -- Stage 4
    # must confirm the answer applied *this* definition, not a looser one.
    disambiguation_required: bool = False


@dataclass
class Stage1Result:
    question_id: str
    question_text: str
    term_matches: List[TermMatch] = field(default_factory=list)
    # The canonical entity-type the question is asking about, e.g.
    # "chain" vs "control" vs "obligation" -- recorded here, consumed by
    # Stage 4's entity-type cross-check. None if the question doesn't
    # anchor on one of the tracked entity nouns.
    entity_type: Optional[str] = None

    @property
    def has_undefined_term(self) -> bool:
        return any(m.kind == MatchKind.NO_MATCH for m in self.term_matches)

    @property
    def needs_disambiguation_check(self) -> bool:
        return any(m.disambiguation_required for m in self.term_matches)


@dataclass
class Stage2Result:
    question_id: str
    question_text: str
    count_shaped: bool = False
    multi_part: bool = False
    # The literal keyword(s)/phrase(s) that triggered each flag, for
    # debugging and for the validation report -- not consumed downstream.
    count_signals: List[str] = field(default_factory=list)
    multi_part_signals: List[str] = field(default_factory=list)


@dataclass
class CheckResult:
    """Uniform result shape for every Stage 4 sub-check."""

    check_name: str
    flagged: bool
    reason: str
    evidence: dict = field(default_factory=dict)


@dataclass
class FitnessResult:
    question_id: str
    checks: List[CheckResult] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not any(c.flagged for c in self.checks)


@dataclass
class ThreeBlockOutput:
    """README.md's "Output posture -- the three-block contract": every
    pipeline response is exactly these three blocks, never a bare answer.
    """

    question_id: str
    confidence_statement: str  # (A)
    answer: str  # (B)
    verification_data: dict = field(default_factory=dict)  # (C)

    @property
    def is_complete(self) -> bool:
        """Success criterion 'Three-block completeness': every output must
        have non-empty (A), (B), and (C) -- never a bare answer."""
        return bool(self.confidence_statement) and bool(self.answer) and bool(self.verification_data)
