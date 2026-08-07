#!/usr/bin/env python3
"""Full 39-question catalog run through QueryMechanismV3's deterministic
stages (v1 template + Candidate D catalog), reporting per-stage attribution,
per q-approach4.md §9 item 6. This does NOT invoke the v2 agent/LLM for
questions that fall through -- those 12 questions already have extensive
live-model evidence in ../query1 (q-approach2.md, direction-correction.md,
union-of-n.md); re-running all of them here would just re-litigate results
already on record. What this script measures freshly is exactly what's
*new*: does the router still correctly handle all 24 v1 questions unchanged,
correctly refuse the 2 schema-gap questions, and route the 4 newly-covered
questions (H1, H5, H9, H11) to the catalog stage instead of the LLM --
without ever falling through to the LLM for a question golden-answers.md
says should be catalog-reachable.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "query1"))

from query_mechanism_v2 import NoLLMConfigured  # noqa: E402

from query_mechanism_v3 import QueryMechanismV3  # noqa: E402

# The same 39 questions test_query_mechanism_v1.py uses (CASES + OUT_OF_SCOPE),
# reproduced here rather than imported so this script doesn't depend on that
# module's internal test structure -- just the (id, question) pairs.
V1_CASES = [
    "S1", "S2", "S3", "S4", "S5", "S6", "S7", "S8", "M1", "M2", "M4", "M6",
    "M7", "M8", "H2", "H4", "H7", "S9", "S10", "M9", "M10", "M11", "M12", "M13",
]

QUESTIONS = [
    ("S1", "What roles does GDPR define?"),
    ("S2", "What's the text of CRA Article 13.1?"),
    ("S3", "What obligations does the Manufacturer role carry under CRA?"),
    ("S4", "What capabilities does 'Maintain Security Logging' require?"),
    ("S5", "When does CRA become effective, and what's its current status?"),
    ("S6", "Which requirement does the 'Maintain Security Logging' obligation satisfy?"),
    ("S7", "What policy governs the 'Security Logging' capability?"),
    ("S8", "List the standards under the Data Protection Policy."),
    ("M1", "Which capabilities are required by more than one obligation?"),
    ("M2", "Trace the full path from CRA Art. 13.1 to whatever capability it ultimately requires."),
    ("M4", "How many obligations does GDPR place on Data Processors vs. Data Controllers?"),
    ("M6", "Which obligations are backed by the weakest extraction confidence?"),
    ("M7", "Show every path from a GDPR requirement down to a Control that verifies it."),
    ("M8", "Which capabilities does our internal Helvex regulation share with CRA?"),
    ("H2", "Which capabilities required by CRA have no governing Policy yet?"),
    ("H4", "Show me the audit evidence that our log retention control passed last quarter."),
    ("H7", "Which of our automated controls are due for review in the next 30 days?"),
    ("S9", "Which Controls exist under the Incident & Vulnerability Response Policy, and what are their statuses?"),
    ("S10", "What's the implementation status of the Encryption-at-Rest control?"),
    ("M9", "How many Controls are currently overdue for review?"),
    ("M10", "What percentage of our Policies are still draft or deprecated rather than approved?"),
    ("M11", "Which Capabilities have a governing Policy but zero implemented Controls underneath?"),
    ("M12", "Which Controls are overdue for review right now?"),
    ("M13", "Which Standards under the Data Protection & Security Policy are still in draft?"),
    ("M3", "Which obligations, across all three loaded regulations, require a 'Security Logging'-type capability?"),
    ("M5", "Do CRA and NIS2 impose obligations on similar roles (e.g. something Manufacturer-like)?"),
    ("H1", "Are we compliant with GDPR Article 32?"),
    ("H3", "Is this new API endpoint, which logs access but doesn't encrypt data at rest, compliant with GDPR Article 32?"),
    ("H5", "NIS2 was updated - which of our Policies are now potentially out of date?"),
    ("H6", "If we adopt a 'Software Bill of Materials' capability, which existing CRA/NIS2 obligations would it newly satisfy?"),
    ("M14", "Which of our draft Policies are blocking GDPR readiness?"),
    ("H8", "I'm building a new microservice that stores customer PII in a database - what compliance capabilities should I be thinking about?"),
    ("H9", "Our security scanner flagged missing rate-limiting on an endpoint that processes health data - does that block a GDPR-relevant control?"),
    ("H10", "Is my service, checkout-api, currently compliant?"),
    ("H11", "If an attacker exploited a missing MFA control today, which regulatory obligations across CRA/NIS2/GDPR would we be out of compliance with?"),
    ("H12", "Across our whole Control set, where are we most exposed - what would an auditor flag first?"),
    ("H13", "Give me a one-paragraph summary of our overall compliance posture I can bring to the board."),
    ("H14", "What should my team prioritize this quarter to move the needle on compliance?"),
    ("H15", "How long, on average, does it take a Standard to go from draft to implemented in our organization?"),
]

EXPECTED_CATALOG = {"H1", "H5", "H9", "H11"}
EXPECTED_SCHEMA_GAP = {"H10", "H15"}  # must NOT be caught by v1 or catalog -- correct behavior is falling through


def main() -> None:
    mech = QueryMechanismV3()
    # Skip actually invoking the v2 LLM agent (NoLLMConfigured by default) --
    # we only need to know WHICH stage each question reaches, not the LLM's
    # answer, which query1 already extensively evidenced for these 12.
    counts = {"v1-template": 0, "v2-catalog": 0, "v2-agent (would need LLM)": 0}
    mismatches = []

    print(f"{'id':<5} {'stage':<28} {'template':<8} question")
    print("-" * 100)
    for qid, question in QUESTIONS:
        try:
            result = mech.v1.ask(question)
            stage, template = "v1-template", result.template
        except Exception:
            catalog = mech.catalog_store.get(mech.v1.graph)
            resolver = mech._get_resolver(catalog)
            matched = None
            for name, pattern, handler in __import__("query_mechanism_v3").CATALOG_TEMPLATES:
                m = pattern.search(question)
                if not m:
                    continue
                try:
                    handler(m, catalog, resolver)
                    matched = name
                    break
                except Exception:
                    continue
            if matched:
                stage, template = "v2-catalog", matched
            else:
                stage, template = "v2-agent (would need LLM)", None

        counts[stage] += 1
        print(f"{qid:<5} {stage:<28} {template or '-':<8} {question[:70]}")

        if qid in V1_CASES and stage != "v1-template":
            mismatches.append(f"{qid}: expected v1-template, got {stage}")
        if qid in EXPECTED_CATALOG and stage != "v2-catalog":
            mismatches.append(f"{qid}: expected v2-catalog, got {stage}")
        if qid in EXPECTED_SCHEMA_GAP and stage == "v2-catalog":
            mismatches.append(f"{qid}: schema-gap question was WRONGLY caught by catalog stage")

    print("\n" + "=" * 60)
    print("Per-stage counts:")
    for stage, n in counts.items():
        print(f"  {stage}: {n}")
    print(f"\nTotal: {sum(counts.values())} (expect 39)")
    print(f"\nLLM-dependent question count: baseline (query1) = 15 (M3,M5,M14,H1,H3,H5,H6,H8,H9,H11,H12,H13,H14 + 2 schema-gap"
          f" refusals also go through v2 to produce an honest 'not tracked' answer) "
          f"vs. this router = {counts['v2-agent (would need LLM)']}")

    if mismatches:
        print(f"\n{len(mismatches)} MISMATCH(ES) vs. expected routing:")
        for m in mismatches:
            print(f"  - {m}")
        sys.exit(1)
    else:
        print("\nAll 39 questions routed exactly as mining-pass.md predicted. No mismatches.")


if __name__ == "__main__":
    main()
