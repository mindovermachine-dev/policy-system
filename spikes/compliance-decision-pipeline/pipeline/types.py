# © 2026 Cartman ApS. All rights reserved.
"""Shared data shapes for the pipeline stages.

Kept deliberately small -- these are what Stage 1/2 produce and what
Stage 4 consumes, not a general-purpose framework. Extend only when a
concrete stage needs a new field, not speculatively.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import FrozenSet, List, Optional


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


class RoutingPath(Enum):
    """Stage 3's four routes (README.md "Stage 3 -- Routing")."""

    DIRECT_CONFIDENT = "direct_confident"
    DIRECT_MANDATORY_CHECK = "direct_mandatory_check"
    DECOMPOSE = "decompose"
    REFUSE = "refuse"


@dataclass
class RoutingDecision:
    """Stage 3's routing decision for one question: which path it takes,
    and -- for the direct-answer paths -- which Stage 4 check(s) are
    mandatory, not optional, before a confident result may be delivered.

    `mandatory_check_names` closes a real gap the composer had without it:
    `FitnessResult.passed` is vacuously True when zero checks were run, so
    a question that needed a specific check but got none would silently
    compose a confident answer with nothing behind it. This set is an
    any-of requirement (at least one of these check_names must appear
    among the checks actually performed), not all-of -- see routing.py's
    module docstring for why v0 has no single canonical grounding check
    per B/D-type question.

    `reason` is the human-legible explanation of why this route was
    chosen -- rendered into (C) for auditability, same discipline as every
    other named mechanism in this pipeline.
    """

    question_id: str
    path: RoutingPath
    mandatory_check_names: FrozenSet[str] = field(default_factory=frozenset)
    reason: str = ""


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
