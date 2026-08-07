#!/usr/bin/env python3
"""Live before/after comparison for the two questions with the most
concretely documented failures in ../query1 (direction-correction.md,
union-of-n.md): H1 (false governance claim, umbrella-clause omission) and
H11 (under-citing 2-of-7 obligations). "Before" = query_mechanism_v2's
freehand agentic loop, exactly as query1 shipped it, single run (not
union-of-3, to keep this fast) against a real local model. "After" =
query_mechanism_v3's Candidate D catalog stage, already shown to match
golden-answers.md exactly in catalog_answers.py.

This is the direct empirical answer to "is Candidate D better," not just an
architectural argument -- same live model, same live graph, same question
text, two mechanisms.
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "query1"))

from query_mechanism_v2 import OllamaClient, QueryMechanismV2  # noqa: E402

from query_mechanism_v3 import QueryMechanismV3  # noqa: E402

MODEL = "qwen3-coder-next:q4_K_M"

QUESTIONS = [
    ("H1", "Are we compliant with GDPR Article 32?"),
    (
        "H11",
        "If an attacker exploited a missing MFA control today, which regulatory obligations "
        "across CRA/NIS2/GDPR would we be out of compliance with?",
    ),
]


def main() -> None:
    llm = OllamaClient(model=MODEL)
    v2 = QueryMechanismV2(llm=llm, union_runs=1)
    v3 = QueryMechanismV3()

    for tag, question in QUESTIONS:
        print(f"\n{'=' * 80}\n{tag}: {question}\n{'=' * 80}")

        print(f"\n--- BEFORE (query1's v2 agent, freehand Cypher, model={MODEL}, single run) ---")
        t0 = time.time()
        try:
            before = v2._ask_agent(question)
            elapsed = time.time() - t0
            print(f"[{elapsed:.1f}s, {len(before.tool_calls_made)} tool calls]")
            print(before.answer)
        except Exception as exc:  # noqa: BLE001
            elapsed = time.time() - t0
            print(f"[{elapsed:.1f}s] FAILED: {exc}")

        print(f"\n--- AFTER (query2's Candidate D catalog stage, deterministic, no LLM) ---")
        t0 = time.time()
        after = v3.ask(question)
        elapsed = time.time() - t0
        print(f"[{elapsed:.3f}s, mechanism={after.mechanism}]")
        print(after.answer)


if __name__ == "__main__":
    main()
