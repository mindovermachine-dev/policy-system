#!/usr/bin/env python3
"""Stress test: does clarifier.py detect question *shapes*, or does it just
pattern-match the 11 literal phrasings from golden-answers.md it was built
and tuned against? Every matcher in clarifier.py is a regex written directly
against those 11 questions -- a real risk of overfitting to the exact
wording of a fixed 39-question catalog rather than the vaguer, more varied
way real users actually ask.

20 new questions, none from golden-answers.md, deliberately mixed:
  - close paraphrases of the 11 known shapes (tests real generalization)
  - genuinely novel global questions with no shape built for them at all
    (tests that the router degrades safely -- falls through, doesn't misfire)
  - phrasings chosen to probe for false positives against the existing
    regexes (a wrong CONFIDENT answer is worse than a safe fallthrough)

`expect` below is a pre-registered guess, written before running this
script, not a post-hoc rationalization -- so a mismatch between `expect`
and the actual result is a real finding, not something to explain away.

Mirrors experiment_full_attribution.py's approach: classify which stage
handles each question WITHOUT blindly invoking the v2 LLM for all 20 (that
would be 20 live model calls just to confirm "falls through," which query1/
query2 already have extensive evidence for). Where a live v2 answer is
actually informative (novel questions with no shape at all), that's called
out explicitly and run separately, not blindly for all 20.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "query2"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "query1"))

from falkordb import FalkorDB  # noqa: E402
from query_mechanism_v1 import NoTemplateMatch, QueryMechanismV1  # noqa: E402

from catalog import compile_catalog  # noqa: E402
from clarifier import route  # noqa: E402
from resolver import CapabilityResolver  # noqa: E402
from query_mechanism_v3 import CATALOG_TEMPLATES  # noqa: E402

QUESTIONS = [
    # -- paraphrases of known "scope-ambiguous, direct" shapes (M3/M14/H6) --
    ("N1", "Which policies still sitting in draft status could delay our GDPR readiness?",
     "clarify-or-direct (M14 paraphrase)"),
    ("N2", "Are there any capabilities anywhere in the graph that look like incident response but aren't required by NIS2?",
     "fallthrough (M3-shaped but inverted direction, not a simple paraphrase)"),
    ("N3", "We're considering adding a data anonymization capability -- does that already overlap with anything tracked under NIS2 or GDPR?",
     "clarify-or-direct (H6 paraphrase)"),
    # -- paraphrase of H3 --
    ("N4", "This new health-data service logs every access but has no audit trail retention -- would it pass GDPR Article 32?",
     "fallthrough or partial (H3-shaped, different clause wording, no 'this ... which' scaffold)"),
    # -- paraphrase of M5 --
    ("N5", "How do CRA's duties for manufacturers stack up against what NIS2 asks of essential entities?",
     "fallthrough (M5 paraphrase, my matcher's regex is narrow: 'impose obligations on similar roles')"),
    # -- paraphrase of H8 --
    ("N6", "We're building an export-your-data feature -- what compliance areas should we be thinking about?",
     "fallthrough (H8 paraphrase, drops the word 'capabilities')"),
    # -- paraphrases of H12/H13/H14 --
    ("N7", "Where are we most exposed right now?",
     "clarify (contains 'most exposed' substring -- should match despite being much shorter)"),
    ("N8", "Draft me a two-sentence compliance status update for leadership.",
     "fallthrough (H13 paraphrase, drops 'board', 'one-paragraph')"),
    ("N9", "What should the security team focus on next sprint?",
     "fallthrough (H14 paraphrase, drops 'prioritize this quarter')"),
    # -- paraphrase of H10 (schema gap) --
    ("N10", "Is checkout-api compliant with our policies?",
     "fallthrough (H10 paraphrase, drops 'currently compliant?' exact ending my regex requires)"),
    # -- paraphrase of H15 (schema gap) --
    ("N11", "On average, how quickly do we move a Standard from draft into production?",
     "fallthrough (H15 paraphrase, 'into production' vs 'to implemented')"),
    # -- paraphrase of H5 (already query2's catalog stage, not clarifier -- tests an EXISTING template) --
    ("N12", "Which of our approved Policies might be stale given how regulations have shifted recently?",
     "fallthrough (tests query2's own H5 catalog regex, not just my new matchers)"),
    # -- genuinely novel, no shape built for these at all --
    ("N13", "If our DPO left tomorrow, what regulatory obligations would suddenly be at risk?",
     "fallthrough (novel: staffing hypothetical, no Person/assignment concept in schema)"),
    ("N14", "Can you give our compliance program a maturity score out of 10?",
     "fallthrough (novel: asks for a metric the graph doesn't compute -- H13's 'don't fabricate a score' territory)"),
    ("N15", "What obligations do we have specifically around AI systems?",
     "fallthrough (novel: no AI Act loaded at all -- tests whether anything falsely claims coverage)"),
    ("N16", "If we started processing biometric data, which capabilities would suddenly become relevant?",
     "fallthrough (H8-shaped hypothetical, vaguer, no fixed phrase match)"),
    ("N17", "Are we ready for an external audit next month?",
     "fallthrough (very vague global question, no anchor, no matching keyword)"),
    ("N18", "What's standing between us and full NIS2 compliance?",
     "fallthrough (M14-shaped for a different regulation, completely different surface form)"),
    ("N19", "Do we have any duplicate or overlapping controls, and are there gaps we're missing?",
     "fallthrough (compound question, two different shapes glued together)"),
    ("N20", "Take me through what it would take for us to be fully compliant with NIS2 end to end.",
     "fallthrough (H1-shaped but for NIS2, not GDPR -- catalog_answers.py's H1 function is GDPR-only by design)"),
]


def classify(question: str, v1: QueryMechanismV1, catalog, resolver) -> tuple[str, str]:
    try:
        result = v1.ask(question)
        return "v1-template", result.template
    except NoTemplateMatch:
        pass
    for name, pattern, handler in CATALOG_TEMPLATES:
        m = pattern.search(question)
        if not m:
            continue
        try:
            handler(m, catalog, resolver)
            return "v2-catalog", name
        except Exception:
            continue
    r = route(question, catalog, resolver)
    if r.kind == "fallthrough":
        return "v2-agent (would need LLM)", None
    return f"v3-{r.kind}", r.shape


def main() -> None:
    db = FalkorDB(host="localhost", port=6379)
    g = db.select_graph("policy_system")
    v1 = QueryMechanismV1()
    catalog = compile_catalog(g)
    resolver = CapabilityResolver(catalog.all_capabilities)

    print(f"{'id':<5} {'stage':<26} {'shape':<10} expect")
    print("-" * 110)
    counts: dict[str, int] = {}
    for qid, question, expect in QUESTIONS:
        stage, shape = classify(question, v1, catalog, resolver)
        counts[stage] = counts.get(stage, 0) + 1
        print(f"{qid:<5} {stage:<26} {(shape or '-'):<10} {expect}")
        print(f"      Q: {question}")
        if stage.startswith("v3-clarify"):
            r = route(question, catalog, resolver)
            print(f"      -> {r.clarification.prompt}")
            for c in r.clarification.choices[:3]:
                print(f"           - {c}")
        elif stage.startswith("v3-direct") or stage.startswith("v3-present") or stage.startswith("v3-refuse"):
            r = route(question, catalog, resolver)
            print(f"      -> {r.answer.splitlines()[0]}")

    print("\n" + "=" * 60)
    print("Per-stage counts across the 20 new questions:")
    for stage, n in sorted(counts.items()):
        print(f"  {stage}: {n}")


if __name__ == "__main__":
    main()
