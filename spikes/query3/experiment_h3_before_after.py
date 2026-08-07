#!/usr/bin/env python3
"""Next Steps item 4: live before/after for H3, same discipline as
../query2/experiment_before_after.py used for H1/H11. "Before" = query1's
unmodified v2 freehand agent, single run, real local model. "After" = the
new clarify-then-answer flow: clarifier.py's pre-filled clause split
(already verified live in clarifier.py's own run to match the expected
capabilities) is treated as auto-confirmed here to exercise the full path
without a human in the loop, then answer_h3_scenario() computes the verdict
deterministically.
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "query2"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "query1"))

from query_mechanism_v2 import OllamaClient, QueryMechanismV2  # noqa: E402

from clarifier import route  # noqa: E402
from query_mechanism_v4 import QueryMechanismV4  # noqa: E402

MODEL = "qwen3-coder-next:q4_K_M"
QUESTION = "Is this new API endpoint, which logs access but doesn't encrypt data at rest, compliant with GDPR Article 32?"


def main() -> None:
    llm = OllamaClient(model=MODEL)
    v2 = QueryMechanismV2(llm=llm, union_runs=1)
    v4 = QueryMechanismV4()

    print(f"{'=' * 80}\nH3: {QUESTION}\n{'=' * 80}")

    print(f"\n--- BEFORE (query1's v2 agent, freehand Cypher, model={MODEL}, single run) ---")
    t0 = time.time()
    try:
        before = v2._ask_agent(QUESTION)
        elapsed = time.time() - t0
        print(f"[{elapsed:.1f}s, {len(before.tool_calls_made)} tool calls]")
        print(before.answer)
    except Exception as exc:  # noqa: BLE001
        elapsed = time.time() - t0
        print(f"[{elapsed:.1f}s] FAILED: {exc}")

    print("\n--- AFTER, step 1: clarifier's default clause split (what the user would be shown to confirm) ---")
    catalog = v4.v3.catalog_store.get(v4.v3.v1.graph)
    resolver = v4.v3._get_resolver(catalog)
    route_result = route(QUESTION, catalog, resolver)
    print(f"kind={route_result.kind}")
    print(route_result.clarification.prompt)
    for c in route_result.clarification.choices:
        print(f"  - {c}")

    print("\n--- AFTER, step 2: auto-confirm the defaults, compute the deterministic verdict (0 LLM calls) ---")
    default_claims = route_result.extracted["default_claims"]
    t0 = time.time()
    after = v4.ask_h3_clarified(QUESTION, default_claims)
    elapsed = time.time() - t0
    print(f"[{elapsed:.4f}s, mechanism={after.mechanism}]")
    print(after.answer)

    print("\n--- Rubric check (golden-answers.md H3) ---")
    checks = {
        "cites Encryption-at-Rest / cap_data_encryption": "cap_data_encryption_0e50d3" in after.answer,
        "concludes NON-COMPLIANT specifically (not vague)": "NON-COMPLIANT" in after.answer,
        "performs NL->Capability mapping explicitly": "cap_access_control_authentication_151816" in after.answer
        and "cap_data_encryption_0e50d3" in after.answer,
    }
    for name, ok in checks.items():
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")


if __name__ == "__main__":
    main()
