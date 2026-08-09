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
2. A Stage 3 `RoutingDecision` was supplied and its path is
   `DIRECT_MANDATORY_CHECK` (types B/C/D), but no Stage 4 check that
   satisfies the route's requirement was actually performed -> gate
   failed. This is what closes the vacuous-pass gap `FitnessResult` has on
   its own (`passed` is trivially True with zero checks run) --
   `pipeline/routing.py`'s module docstring has the full rationale.
   Enforcement is keyed on the *path*, not on whether `mandatory_check_
   names` happens to be non-empty: an earlier version of this gate keyed
   on the named-checks set directly, which left a real hole for any B/C/D
   question landing in a "no check named yet -- documented gap" routing
   branch (confirmed live: AU-H2 composed with zero checks produced a
   false confident result before this fix -- see PROGRESS.md's "Live
   held-out generalization audit" follow-up). When `mandatory_check_names`
   is non-empty, one of those specific names must appear; when it's empty,
   any real Stage 4 check counts (weaker, but closes the zero-checks hole,
   which is the part that actually violates "no false auto-pass"). `routing`
   is optional and backward compatible: omitting it (as callers predating
   Stage 3 do) skips this check entirely, same behavior as before Stage 3
   existed.
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
from .types import FitnessResult, MatchKind, RoutingDecision, RoutingPath, Stage1Result, Stage2Result, ThreeBlockOutput


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

    if routing is not None and routing.path == RoutingPath.DIRECT_MANDATORY_CHECK:
        # Enforcement is keyed on the *path*, not on whether routing named
        # specific check(s) -- README's Stage 3 makes mandatory verification
        # a property of the type (B/C/D), not of whether routing.py happens
        # to know how to name the right check yet. Keying this on
        # `routing.mandatory_check_names` alone (the original version of
        # this gate) left a real hole: any B/C/D question landing in a
        # "no check named -- documented gap" branch (e.g. AU-H2, type D,
        # not the hypothetical-chain shape) had an empty mandatory set, so
        # the old condition skipped enforcement entirely and let
        # FitnessResult.passed's zero-checks vacuous-True through --
        # confirmed live: AU-H2 composed with zero Stage 4 checks produced
        # a confident "this is correct" before this fix. See PROGRESS.md.
        performed = {c.check_name for c in fitness.checks}
        if routing.mandatory_check_names:
            satisfied = bool(performed & routing.mandatory_check_names)
            requirement = (
                f"at least one of {sorted(routing.mandatory_check_names)} to have "
                f"been performed ({routing.reason})"
            )
        else:
            # No specific check is named for this route yet (a documented
            # gap, not a silent one -- see routing.py) -- but mandatory
            # verification still applies, so *some* real Stage 4 check must
            # have run. Weaker than the named-check case (doesn't confirm
            # the check performed is actually the relevant one), but it
            # closes the zero-checks hole, which is the failure mode that
            # actually violates "no false auto-pass".
            satisfied = bool(performed)
            requirement = f"at least one Stage 4 check to have been performed ({routing.reason})"
        if not satisfied:
            confidence = (
                f"Fitness gate failed: this question's route requires {requirement}, "
                "but none was. A check set that doesn't meet the mandatory-verification "
                "requirement must not be treated as a pass -- see (C) for exactly what "
                "was (and wasn't) run."
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
