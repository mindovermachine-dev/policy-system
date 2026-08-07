#!/usr/bin/env python3
"""query3's router -- q-approach5.md §6's design, Next Steps item 6 ("wire it
all into a v4 router"). Extends query2's QueryMechanismV3 (v1 template ->
Candidate D catalog -> v2 agent) with one new stage in between:

  1. v1 template router (unchanged, from query1)
  2. Candidate D catalog lookup (unchanged, from query2)
  3. NEW -- clarifier.route(): hard-tier shape matcher (clarifier.py).
     "direct" -> answers deterministically via catalog_answers_v4.py, no LLM.
     "present_and_stop" -> returns the real comparison table, no verdict.
     "clarify" -> returns a ClarificationRequest instead of an answer; the
       caller is expected to re-ask with the clarified question (or, for the
       prototype CLI below, auto-accept the pre-filled default so the whole
       flow can be exercised end-to-end without a human in the loop).
     "refuse" -> schema-gap refusal, names the missing concept.
     "fallthrough" -> nothing matched; falls to stage 4.
  4. v2's existing freehand agentic loop (unchanged) -- floor of last resort.

This does NOT claim stage 3 covers more than q-approach5.md's own measured
scope: only M3, M14, H6 go fully "direct" without a round-trip (their
regulation/capability is already named in the question text); H3, H8,
H12-H14 return "clarify" and need a second call with the clarification
answer folded in; M5 stops at a table; H10/H15 refuse. Nothing here
overclaims what was verified in verify_remaining_rows.py or
q-approach5.md itself.
"""

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "query2"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "query1"))

from query_mechanism_v2 import LLMClient, NoLLMConfigured  # noqa: E402
from query_mechanism_v3 import MechanismResult, QueryMechanismV3  # noqa: E402

from catalog_answers_v4 import answer_h12_14_prioritized, answer_h3_scenario  # noqa: E402
from clarifier import ClarificationChoice, RouteResult, route  # noqa: E402


@dataclass
class V4Result(MechanismResult):
    clarification: Optional[ClarificationChoice] = None


class QueryMechanismV4:
    def __init__(self, host="localhost", port=6379, graph_name="policy_system",
                 llm: Optional[LLMClient] = None, union_runs: int = 3):
        self.v3 = QueryMechanismV3(host=host, port=port, graph_name=graph_name, llm=llm or NoLLMConfigured(),
                                    union_runs=union_runs)

    def ask(self, question: str) -> V4Result:
        # Stages 1-2: try v1 template + Candidate D catalog first, exactly as
        # query2 shipped -- stage 3 only exists for what those two couldn't
        # already answer. Cheapest to run: try the fast path, catch its
        # internal fallthrough by calling v3 directly only when needed. Since
        # QueryMechanismV3.ask() always falls through to v2 on a miss, reach
        # into its stage 1/2 logic directly here instead of calling .ask().
        from query_mechanism_v1 import NoTemplateMatch
        try:
            v1_result = self.v3.v1.ask(question)
            summary = "; ".join(f"{v1_result.columns}: {row}" for row in v1_result.rows[:20])
            return V4Result(question=question, mechanism="v1-template", answer=summary or "(no rows)",
                             template=v1_result.template)
        except NoTemplateMatch:
            pass

        catalog = self.v3.catalog_store.get(self.v3.v1.graph)
        resolver = self.v3._get_resolver(catalog)
        for name, pattern, handler in __import__("query_mechanism_v3").CATALOG_TEMPLATES:
            m = pattern.search(question)
            if not m:
                continue
            try:
                answer = handler(m, catalog, resolver)
            except Exception:
                continue
            return V4Result(question=question, mechanism="v2-catalog", answer=answer, template=name)

        # Stage 3: NEW -- hard-tier clarifier.
        result: RouteResult = route(question, catalog, resolver)
        if result.kind == "direct":
            return V4Result(question=question, mechanism="v3-clarified", answer=result.answer, template=result.shape)
        if result.kind == "present_and_stop":
            return V4Result(question=question, mechanism="v3-table-only", answer=result.answer, template=result.shape)
        if result.kind == "refuse":
            return V4Result(question=question, mechanism="v3-refuse", answer=result.answer, template=result.shape)
        if result.kind == "clarify":
            return V4Result(question=question, mechanism="v3-needs-clarification", answer="(clarification required)",
                             template=result.shape, clarification=result.clarification)

        # Stage 4: fallthrough to v2's existing agentic loop, unchanged.
        v2_result = self.v3.v2._ask_agent_union(question)
        return V4Result(question=question, mechanism="v2-agent", answer=v2_result.answer,
                         tool_calls_made=v2_result.tool_calls_made, runs_sampled=v2_result.runs_sampled)

    def ask_h3_clarified(self, question: str, claims: list[tuple[str, bool]]) -> V4Result:
        """Re-entry point for H3 once the clarification round-trip is answered."""
        catalog = self.v3.catalog_store.get(self.v3.v1.graph)
        resolver = self.v3._get_resolver(catalog)
        answer = answer_h3_scenario(catalog, resolver, claims)
        return V4Result(question=question, mechanism="v3-clarified", answer=answer, template="H3")

    def ask_h12_14_clarified(self, question: str, axis: str) -> V4Result:
        catalog = self.v3.catalog_store.get(self.v3.v1.graph)
        answer = answer_h12_14_prioritized(catalog, axis)
        return V4Result(question=question, mechanism="v3-clarified", answer=answer, template="H12-H14")


if __name__ == "__main__":
    mech = QueryMechanismV4()
    question = sys.argv[1] if len(sys.argv) > 1 else "Which of our draft Policies are blocking GDPR readiness?"
    result = mech.ask(question)
    print(f"mechanism: {result.mechanism}" + (f" (template={result.template})" if result.template else ""))
    if result.clarification:
        print(f"NEEDS CLARIFICATION: {result.clarification.prompt}")
        for c in result.clarification.choices:
            print(f"  - {c}")
    else:
        print(result.answer)
