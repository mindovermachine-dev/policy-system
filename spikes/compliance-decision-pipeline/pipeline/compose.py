# © 2026 Cartman ApS. All rights reserved.
"""The three-block output composer (README.md, "Output posture -- the
three-block contract"). Wires Stage 1 + Stage 2 + Stage 3 + Stage 4
results for a single question into one ThreeBlockOutput -- (A) confidence
statement, (B) answer, (C) verification data.

Routing logic implemented here, per README's Stage 3 + Output posture
sections:

1. Stage 1 found an undefined term -> refuse, name the term. (Currently a
   dead branch in practice: alias_table.check_terms() never returns a
   NO_MATCH entry today -- see its docstring -- but Stage1Result already
   carries has_undefined_term for exactly this case, so the branch is kept
   live for when Stage 1's vocabulary grows beyond the curated cluster.)
2. A Stage 3 `RoutingDecision` was supplied and named mandatory check(s),
   but none of them appear among the Stage 4 checks actually performed ->
   gate failed. This is what closes the vacuous-pass gap `FitnessResult`
   has on its own (`passed` is trivially True with zero checks run) --
   `pipeline/routing.py`'s module docstring has the full rationale and the
   concrete case (AU-M4) this catches. `routing` is optional and backward
   compatible: omitting it (as callers predating Stage 3 do) skips this
   check entirely, same behavior as before Stage 3 existed.
3. Any Stage 4 check flagged -> gate failed. Never deliver the flagged
   claim as verified -- mark it explicitly, state why, and let (C) carry
   the contradicting evidence. This is what "No false auto-pass" (README
   Success Criteria) looks like at the output layer.
4. Otherwise -> (A) comes from the question's type-reliability entry
   (pipeline/question_types.py). Types F and G get a hedge instead of a
   confident statement, unconditionally, per README.

(C) is Stage 4's already-computed evidence, rendered instead of staying
internal-only (README: "these aren't two builds"). It now also carries
`source_refs` -- the other half of (C)'s contract, resolved via
`pipeline/provenance.py` for every Obligation/Requirement id that shows up
in a fitness check's evidence. Scoped deliberately to those two id shapes
only; see provenance.py's module docstring for why Control/Capability ids
are not resolved to a regulation citation here (verified live: doing so
reintroduces the exact over-citation problem `check_regulation_scope`'s
narrowing discipline exists to catch, not a simple omission).
"""

from __future__ import annotations

from typing import List, Optional

from . import provenance
from .question_types import confidence_statement_for_type
from .types import FitnessResult, MatchKind, RoutingDecision, Stage1Result, Stage2Result, ThreeBlockOutput


def _collect_candidate_entity_ids(fitness: FitnessResult) -> List[str]:
    """Every string value (or string inside a list/set value) across every
    fitness check's evidence dict -- a superset of the ids that actually
    carry a source_ref. `provenance.resolve_source_refs` silently skips
    anything that isn't an Obligation/Requirement id, so over-collecting
    here is safe and simpler than special-casing which evidence key means
    what per check."""
    candidates = set()
    for check in fitness.checks:
        for value in check.evidence.values():
            if isinstance(value, str):
                candidates.add(value)
            elif isinstance(value, (list, set, tuple)):
                candidates.update(v for v in value if isinstance(v, str))
    return sorted(candidates)


def _build_verification_data(
    stage1: Stage1Result, stage2: Stage2Result, fitness: FitnessResult, routing: Optional[RoutingDecision]
) -> dict:
    source_refs = provenance.resolve_source_refs(_collect_candidate_entity_ids(fitness))
    return {
        "entity_type": stage1.entity_type,
        "term_matches": [
            {
                "surface_text": m.surface_text,
                "canonical_term": m.canonical_term,
                "kind": m.kind.value,
                "definition": m.definition,
                "disambiguation_required": m.disambiguation_required,
            }
            for m in stage1.term_matches
        ],
        "structural_flags": {
            "count_shaped": stage2.count_shaped,
            "multi_part": stage2.multi_part,
        },
        "fitness_checks": [
            {
                "check_name": c.check_name,
                "flagged": c.flagged,
                "reason": c.reason,
                "evidence": c.evidence,
            }
            for c in fitness.checks
        ],
        "source_refs": source_refs,
        "routing": (
            {
                "path": routing.path.value,
                "mandatory_check_names": sorted(routing.mandatory_check_names),
                "reason": routing.reason,
            }
            if routing is not None
            else None
        ),
    }


def compose_output(
    question_id: str,
    question_type: str,
    stage1: Stage1Result,
    stage2: Stage2Result,
    fitness: FitnessResult,
    proposed_answer: str,
    routing: Optional[RoutingDecision] = None,
) -> ThreeBlockOutput:
    verification_data = _build_verification_data(stage1, stage2, fitness, routing)

    if stage1.has_undefined_term:
        undefined: List[str] = sorted(
            m.surface_text for m in stage1.term_matches if m.kind == MatchKind.NO_MATCH
        )
        confidence = (
            f"Refused: term(s) {undefined} are not defined in the canonical "
            "vocabulary. Not attempting synthesis -- see (C) for what Stage 1 "
            "could and could not match."
        )
        answer = f"Not determinable from what's in the system without a definition for {undefined}."
        return ThreeBlockOutput(question_id, confidence, answer, verification_data)

    if routing is not None and routing.mandatory_check_names:
        performed = {c.check_name for c in fitness.checks}
        if not (performed & routing.mandatory_check_names):
            confidence = (
                f"Fitness gate failed: this question's route requires at least one of "
                f"{sorted(routing.mandatory_check_names)} to have been performed "
                f"({routing.reason}), but none was. A check set that doesn't include the "
                "mandatory mechanism must not be treated as a pass -- see (C) for exactly "
                "what was (and wasn't) run."
            )
            answer = f"[FLAGGED -- not verified] {proposed_answer}"
            return ThreeBlockOutput(question_id, confidence, answer, verification_data)

    if not fitness.passed:
        reasons = "; ".join(c.reason for c in fitness.checks if c.flagged)
        confidence = (
            f"Fitness gate failed: {reasons}. This answer does not meet the "
            "verification bar and must not be treated as confirmed -- see (C) "
            "for the independently re-derived evidence that contradicts it."
        )
        answer = f"[FLAGGED -- not verified] {proposed_answer}"
        return ThreeBlockOutput(question_id, confidence, answer, verification_data)

    confidence = confidence_statement_for_type(question_type)
    answer = f"[Draft, unverified] {proposed_answer}" if question_type in ("F", "G") else proposed_answer
    return ThreeBlockOutput(question_id, confidence, answer, verification_data)
