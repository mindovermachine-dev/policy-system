#!/usr/bin/env python3
"""Approach 3 (query2): the fixed-order router from q-approach4.md's final
design (§8), Candidate B removed per mining-pass.md's measured no-go:

  1. query_mechanism_v1's template router -- unchanged.
  2. NEW -- Candidate D: pre-compiled catalog lookup, deterministic, no LLM.
     Covers H1, H5, H9, H11 (mining-pass.md's "fully reachable" set).
  3. query_mechanism_v2's existing agentic loop -- unchanged, as the floor
     for everything stage 1/2 couldn't confidently handle (M3, M5, M14, H3,
     H6, H8, H12, H13, H14; also anything stage 2 fails to resolve for H1/
     H5/H9/H11 themselves, e.g. an unresolvable free-text term).

No Candidate B (DSL-mediated agentic loop): mining-pass.md's mining pass
found every golden query is a slice of the same two chains the catalog
already materializes, so there is no live-traversal shape left for a DSL to
compile once Candidate D exists -- the go/no-go gate from q-approach4.md §7
fix 9 resolved to no-go before any code was written for it, not after.

No Candidate C (cross-model verification) in the router itself --
experiment_cross_model_verification.py tests it standalone per §7 fix 3;
it's wired in here only if that experiment shows a real catch.
"""

import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "query1"))

from query_mechanism_v1 import NoTemplateMatch, QueryMechanismV1  # noqa: E402
from query_mechanism_v2 import LLMClient, NoLLMConfigured, QueryMechanismV2  # noqa: E402

from catalog import CatalogStore  # noqa: E402
from catalog_answers import (  # noqa: E402
    NoConfidentMatch,
    answer_h1_gdpr_art32,
    answer_h5_nis2_staleness,
    answer_h9_rate_limiting,
    answer_h11_mfa_reverse,
)
from resolver import CapabilityResolver  # noqa: E402


class NoCatalogMatch(Exception):
    pass


def _h_catalog_h1(m, catalog, resolver):
    reg = m.group(1).upper()
    article = m.group(2)
    rows_exist = any(
        r.requirement_id and r.requirement_id.startswith(f"{reg}-1.0_req_art_{article}") for r in catalog.rows
    )
    if not rows_exist:
        raise NoCatalogMatch(f"no requirement rows for {reg} article {article}")
    if reg != "GDPR":
        # Only H1's exact verdict-classification shape has been built/graded
        # against a golden rubric (see catalog_answers.py) -- extending it to
        # every regulation without a golden case to check against would be
        # an unverified generalization, not a tested capability. Falls
        # through to v2 rather than guessing.
        raise NoCatalogMatch(f"H1-shaped verdict logic only built+graded for GDPR, not {reg}")
    return answer_h1_gdpr_art32(catalog)


def _h_catalog_h11(m, catalog, resolver):
    free_text = m.group(1).strip()
    return answer_h11_mfa_reverse(catalog, resolver, free_text)


def _h_catalog_h5(m, catalog, resolver):
    return answer_h5_nis2_staleness(catalog)


def _h_catalog_h9(m, catalog, resolver):
    free_text = m.group(1).strip()
    return answer_h9_rate_limiting(resolver, free_text)


CATALOG_TEMPLATES = [
    # Anchored to "are we compliant" specifically -- NOT just "compliant
    # with X article N" anywhere in the question. Caught empirically by
    # experiment_full_attribution.py: the unanchored pattern also matched
    # H3 ("Is THIS new API endpoint... compliant with GDPR Article 32?"),
    # a scenario-scoped question with a different, two-capability answer
    # shape -- and would have silently returned H1's org-wide verdict for
    # it, a wrong answer that looks plausible. This is exactly the "more
    # stages, more surface for a silent bug" risk q-approach4.md's own
    # critique (§6 point 1) named in the abstract; this is that failure
    # mode caught concretely, once, by actually running all 39 questions
    # through the router rather than only the 4 it was designed for.
    ("H1", re.compile(r"\bare we compliant with (\w+) article\s*([\d.]+)", re.I), _h_catalog_h1),
    ("H11", re.compile(r"missing ([\w\s\-]+?) control today", re.I), _h_catalog_h11),
    ("H5", re.compile(r"which of our policies are now potentially out of date", re.I), _h_catalog_h5),
    ("H9", re.compile(r"flagged missing ([\w\s\-]+?) on an endpoint", re.I), _h_catalog_h9),
]


@dataclass
class MechanismResult:
    question: str
    mechanism: str  # "v1-template" | "v2-catalog" | "v2-agent"
    answer: str
    template: Optional[str] = None
    tool_calls_made: list = field(default_factory=list)
    runs_sampled: int = 1


class QueryMechanismV3:
    def __init__(
        self,
        host="localhost",
        port=6379,
        graph_name="policy_system",
        llm: Optional[LLMClient] = None,
        union_runs: int = 3,
    ):
        self.v1 = QueryMechanismV1(host=host, port=port, graph_name=graph_name)
        self.v2 = QueryMechanismV2(
            host=host, port=port, graph_name=graph_name, llm=llm or NoLLMConfigured(), union_runs=union_runs
        )
        self.catalog_store = CatalogStore()
        self._resolver: Optional[CapabilityResolver] = None
        self._resolver_signature: Optional[str] = None

    def _get_resolver(self, catalog) -> CapabilityResolver:
        if self._resolver is None or self._resolver_signature != catalog.signature:
            self._resolver = CapabilityResolver(catalog.all_capabilities)
            self._resolver_signature = catalog.signature
        return self._resolver

    def ask(self, question: str) -> MechanismResult:
        try:
            v1_result = self.v1.ask(question)
            summary = "; ".join(f"{v1_result.columns}: {row}" for row in v1_result.rows[:20])
            return MechanismResult(
                question=question, mechanism="v1-template", answer=summary or "(no rows)", template=v1_result.template
            )
        except NoTemplateMatch:
            pass

        catalog = self.catalog_store.get(self.v1.graph)
        resolver = self._get_resolver(catalog)
        for name, pattern, handler in CATALOG_TEMPLATES:
            m = pattern.search(question)
            if not m:
                continue
            try:
                answer = handler(m, catalog, resolver)
            except (NoCatalogMatch, NoConfidentMatch):
                continue  # this template matched the question's shape but not its content -- fall through
            return MechanismResult(question=question, mechanism="v2-catalog", answer=answer, template=name)

        v2_result = self.v2._ask_agent_union(question)
        return MechanismResult(
            question=question,
            mechanism="v2-agent",
            answer=v2_result.answer,
            tool_calls_made=v2_result.tool_calls_made,
            runs_sampled=v2_result.runs_sampled,
        )


if __name__ == "__main__":
    mech = QueryMechanismV3()
    question = sys.argv[1] if len(sys.argv) > 1 else "Are we compliant with GDPR Article 32?"
    result = mech.ask(question)
    print(f"mechanism: {result.mechanism}" + (f" (template={result.template})" if result.template else ""))
    print(result.answer)
