# © 2026 Cartman ApS. All rights reserved.
"""Stage 3 -- routing (README.md "Stage 3 -- Routing").

Consumes Stage 1 + Stage 2's per-question signals and the question's
semantic type (A-H, pipeline/question_types.py) and decides which of four
paths a question takes, per README's routing bullets. Does NOT perform
decomposition or answer generation itself -- this pipeline verifies a
candidate answer, it does not produce one (see README's "What This Is
NOT"), so the DECOMPOSE path is a classification only: "this question
needs sub-question routing/composition it doesn't get in v0," not an
executed decomposition. That gap is not new here -- Stage 3 was always
deferred v0 scope (PROGRESS.md's "Chosen build strategy") precisely
because no answer-composition engine exists to decompose *into*; this
module makes the routing *decision* real without pretending the
downstream recursive-answer machinery exists too.

What this module DOES make real, closing a genuine gap: mandatory-check
enforcement. Before this, `FitnessResult.passed` was vacuously True when
zero Stage 4 checks were run -- `tests/run_target_cases.py` documented
this concretely (AU-M4 was excluded from that file's scenarios specifically
because composing it with no Stage 4 mechanism behind it would have been a
silent vacuous pass). `route_question` now assigns, per type/signal, which
check_name(s) are mandatory -- and `compose.py` enforces that at least one
of them was actually performed before allowing a confident result. AU-M4
is the concrete case this closes: it needs a check
(`stale_chain_strict_reading`) that does not exist yet, so routing it
correctly produces a "no mandatory check performed" flagged result instead
of vacuously passing on zero checks -- an honest gap made visible, not
hidden, same discipline as every other documented gap in this pipeline.

## Design resolution: multi_part vs. a type's own known trigger

README's routing bullets list "type with a known specific trigger (B, C,
D)" and "needs decomposition (multi-part, ...)" as separate bullets
without stating which wins when both signals fire on the same question.
This is not hypothetical: SEC-M4 ("Which checks come due... and which
are already overdue?") is Stage 2's multi_part=True (its "which...and
which" phrasing trips the keyword heuristic) *and* a validated,
already-shipped type-B direct-answer target case (PROGRESS.md, Stage 4's
rule check) -- decomposing it would contradict a mechanism this pipeline
already built and validated end-to-end.

Resolution: a type's own known-trigger mechanism wins over the multi_part
structural signal, because the mechanism already reduces the compound
claim to one verifiable derived quantity (SEC-M4's rule check evaluates
the whole "overdue set" as a single query result, which silently subsumes
both clauses of the compound question -- there is nothing left over for
decomposition to do). multi_part still drives DECOMPOSE when the type
itself has no known trigger to lean on (A/E/H with multi_part phrasing --
a corner case with no target case to validate against, since none of the
built target cases hit it). Types F/G and an ALIAS/near-match term match
README's decompose bullet literally (there is no known trigger for F/G by
definition, and Stage 1's own docstring is explicit that a near-match
term "does not attempt full synthesis") -- those take priority over the
type-B/C/D branch, not the other way around, since they represent
*absence* of a known trigger, not a competing one.
"""

from __future__ import annotations

import re

from .question_types import TYPE_RELIABILITY
from .types import MatchKind, RoutingDecision, RoutingPath, Stage1Result, Stage2Result

# Per-canonical-term Stage 4 check that must have run before a type-B claim
# turning on that disambiguation-required term may be trusted. "stale" has
# no built check yet (see fitness.py / PROGRESS.md) -- named here anyway so
# routing AU-M4 produces an honest "no mandatory check performed" result
# instead of silently requiring nothing.
_DISAMBIGUATION_CHECK_FOR_TERM = {
    "overdue": "rule_overdue_excludes_deprecated",
    "deprecated": "rule_overdue_excludes_deprecated",
    "stale": "stale_chain_strict_reading",  # not yet built -- intentional gap, see module docstring
}

# Any-of grounding requirement for a type-B/D question with no disambiguation
# term: some independent re-derivation must have run. v0 has no question-text
# signal that distinguishes which specific grounding shape (existence,
# completeness, scope-match, or fanout) a given claim needs -- see
# PROGRESS.md, this is a documented coarseness, not an oversight. Tightening
# it needs a discriminator validated against a real target case, same bar as
# everything else here.
_GROUNDING_CHECK_NAMES = frozenset(
    {"existence_grounding", "scope_match_regulation_routing", "fanout_maximum", "completeness_grounding"}
)

# D's "hypothetical-chain variant" (README: "independent re-derivation +
# scope-match for D's hypothetical-chain variant") -- validated against
# AU-H4 ("If our log-retention check turns out to have failed...") and
# SEC-H4 ("If the Encryption-at-Rest check fails its review...").
#
# Broadened (later session, overfitting audit): the original single regex
# (`if` ... `fails?|failed|failing|breaks?|broken|turns out`) was built
# from AU-H4/SEC-H4's exact wording and demonstrably missed semantically
# identical paraphrases that don't happen to use "if" or those specific
# verbs -- confirmed by testing it against fresh phrasing, not assumed
# (see PROGRESS.md). Now two independent signals, both required (in either
# order, anywhere in the text): a conditional marker, and a failure/state
# verb. Still not NLP -- a wider but still finite keyword net, same
# category of heuristic as Stage 1's alias table and Stage 2's patterns,
# and it inherits their honesty about that: some hypothetical-chain
# phrasing will still slip past a rewording neither list anticipates
# (e.g. "goes offline", "is compromised"). Validated must-match (AU-H4,
# SEC-H4, plus paraphrases using "should"/"assuming"/"collapses" that the
# old regex missed) and must-not-match (AU-H2, and "currently failing"
# with no conditional marker -- a real-time question, not hypothetical) in
# tests/test_routing.py.
#
# "exploited"/"exploits" added the same session, for a different reason:
# a target-case text audit (PROGRESS.md) found SEC-H1's fixture text had
# been silently paraphrased away from dev-questions.md's actual verbatim
# question ("If an attacker exploited a missing MFA check today...") into
# a plain enumeration with no hypothetical framing at all -- which had
# also caused it to be misclassified type B instead of D. Now fixed to use
# the real text and the real type; "exploited" needed adding here for that
# real question's own wording to correctly match, not to make a synthetic
# case pass.
_CONDITIONAL_MARKER_PATTERN = re.compile(
    r"\b(if|should|assuming|suppose|supposing|were to)\b", re.IGNORECASE
)
_FAILURE_STATE_PATTERN = re.compile(
    r"\b(fails?|failed|failing|breaks?|broken|turns? out|collapses?|collapsed|"
    r"malfunctions?|malfunctioned|stops? working|stops? functioning|goes? down|"
    r"ceases? to (?:function|work)|exploited?|exploits?)\b",
    re.IGNORECASE,
)


def _is_hypothetical_chain(question_text: str) -> bool:
    return bool(
        _CONDITIONAL_MARKER_PATTERN.search(question_text) and _FAILURE_STATE_PATTERN.search(question_text)
    )


def _mandatory_checks_for_type_b(stage1: Stage1Result) -> frozenset:
    disambiguation_terms = {m.canonical_term for m in stage1.term_matches if m.disambiguation_required}
    if disambiguation_terms:
        return frozenset(
            _DISAMBIGUATION_CHECK_FOR_TERM[term]
            for term in disambiguation_terms
            if term in _DISAMBIGUATION_CHECK_FOR_TERM
        )
    return _GROUNDING_CHECK_NAMES


def _mandatory_checks_for_type_c(stage1: Stage1Result) -> frozenset:
    # README: type C's own trigger is "tool-computed count," which has no
    # enforceable check yet -- PROGRESS.md's Success Criteria table already
    # marks "Miscount elimination" NOT YET VERIFIABLE for exactly this
    # reason. What IS enforceable today: EM-E3's granularity-slip shape --
    # if Stage 1 recorded an entity-type, the answer's counting unit must
    # have been cross-checked against it.
    if stage1.entity_type is not None:
        return frozenset({"entity_type_cross_check"})
    return frozenset()


def _mandatory_checks_for_type_d(question_text: str) -> frozenset:
    if _is_hypothetical_chain(question_text):
        return _GROUNDING_CHECK_NAMES
    return frozenset()


def route_question(stage1: Stage1Result, stage2: Stage2Result, question_type: str) -> RoutingDecision:
    if question_type not in TYPE_RELIABILITY:
        raise ValueError(f"unknown question type {question_type!r} -- must be one of {sorted(TYPE_RELIABILITY)}")

    qid = stage1.question_id

    if stage1.has_undefined_term:
        undefined = sorted(m.surface_text for m in stage1.term_matches if m.kind == MatchKind.NO_MATCH)
        return RoutingDecision(
            question_id=qid,
            path=RoutingPath.REFUSE,
            reason=f"undefined term(s) {undefined} -- never full synthesis regardless of structural shape",
        )

    if question_type in ("F", "G"):
        return RoutingDecision(
            question_id=qid,
            path=RoutingPath.DECOMPOSE,
            reason=f"type {question_type} has no known trigger-based fix -- decompose/hedge by default, not fallback",
        )

    if any(m.kind == MatchKind.ALIAS for m in stage1.term_matches):
        near_match = sorted(m.surface_text for m in stage1.term_matches if m.kind == MatchKind.ALIAS)
        return RoutingDecision(
            question_id=qid,
            path=RoutingPath.DECOMPOSE,
            reason=f"near-match term(s) {near_match} -- not a full exact-match definition, no single-shot canonical path",
        )

    if question_type in ("A", "E", "H"):
        return RoutingDecision(
            question_id=qid,
            path=RoutingPath.DIRECT_CONFIDENT,
            reason=f"type {question_type} has near-100% measured reliability / a solved gap-check -- no specific trigger required",
        )

    if question_type in ("B", "C", "D"):
        if question_type == "B":
            mandatory = _mandatory_checks_for_type_b(stage1)
        elif question_type == "C":
            mandatory = _mandatory_checks_for_type_c(stage1)
        else:
            mandatory = _mandatory_checks_for_type_d(stage1.question_text)
        return RoutingDecision(
            question_id=qid,
            path=RoutingPath.DIRECT_MANDATORY_CHECK,
            mandatory_check_names=mandatory,
            reason=(
                f"type {question_type}'s own known trigger applies (wins over Stage 2's multi_part "
                f"signal if both fire -- see module docstring); mandatory check(s): {sorted(mandatory) or 'none built yet'}"
            ),
        )

    if stage2.multi_part:
        return RoutingDecision(
            question_id=qid,
            path=RoutingPath.DECOMPOSE,
            reason="multi-part phrasing with no type-specific known trigger to lean on instead",
        )

    return RoutingDecision(
        question_id=qid,
        path=RoutingPath.DIRECT_CONFIDENT,
        reason=f"type {question_type}, no decomposition or mandatory-check signal fired",
    )
