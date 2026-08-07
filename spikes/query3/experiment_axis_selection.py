#!/usr/bin/env python3
"""Next Steps item 5: does an axis-selection clarification step actually
help H13/H14, or is a single best-effort synthesis (query2's unmodified v2
agent) the right shape for these two specifically -- q-approach5.md §7
critique point 3 flagged this as untested, not assumed. "Before" = v2's
existing freehand agent, single live run, real model, exactly as query1/
query2 shipped it. "After" = the clarified, axis-ranked deterministic
answer from catalog_answers_v4.answer_h12_14_prioritized(), one call per
axis, 0 LLM calls, checked against golden-answers.md's stated rubric items
for H13/H14.
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "query2"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "query1"))

from query_mechanism_v2 import OllamaClient, QueryMechanismV2  # noqa: E402

from query_mechanism_v4 import QueryMechanismV4  # noqa: E402

MODEL = "qwen3-coder-next:q4_K_M"

QUESTIONS = [
    ("H13", "Give me a one-paragraph summary of our overall compliance posture I can bring to the board."),
    ("H14", "What should my team prioritize this quarter to move the needle on compliance?"),
]

# golden-answers.md's own stated rubric-required items, checked as substring
# hits against each answer -- not a vibe check.
RUBRIC_ITEMS = {
    "H13": {
        "68 total capabilities": "68",
        "13 governed": "13",
        "55 ungoverned": "55",
        "1 overdue control": "overdue",
    },
    "H14": {
        "planned Vulnerability Patch SLA Check": "ctrl_std_pol_incident_vulnerability_response_policy_9de859_v2_automated",
        "overdue Incident Triage SLA control": "ctrl_std_pol_incident_vulnerability_response_policy_9de859_v1_manual",
        "draft Clinical Data Integrity Policy": "pol_clinical_data_integrity_policy_e1a539",
        "deprecated Legacy Asset & Personnel Security Policy": "pol_legacy_asset_personnel_security_policy_7ed6c2",
    },
}


def grade(answer: str, items: dict[str, str]) -> None:
    for label, needle in items.items():
        hit = needle.lower() in answer.lower()
        print(f"    [{'PASS' if hit else 'FAIL'}] mentions {label} ({needle!r})")


def main() -> None:
    llm = OllamaClient(model=MODEL)
    v2 = QueryMechanismV2(llm=llm, union_runs=1)
    v4 = QueryMechanismV4()

    for tag, question in QUESTIONS:
        print(f"\n{'=' * 80}\n{tag}: {question}\n{'=' * 80}")

        print(f"\n--- BEFORE (v2 agent, single best-effort synthesis, model={MODEL}) ---")
        t0 = time.time()
        try:
            before = v2._ask_agent(question)
            elapsed = time.time() - t0
            print(f"[{elapsed:.1f}s, {len(before.tool_calls_made)} tool calls]")
            print(before.answer)
            print("  rubric check:")
            grade(before.answer, RUBRIC_ITEMS[tag])
        except Exception as exc:  # noqa: BLE001
            elapsed = time.time() - t0
            print(f"[{elapsed:.1f}s] FAILED: {exc}")

        print("\n--- AFTER (axis-clarified deterministic ranking, 0 LLM calls, one call per axis) ---")
        for axis in ["review_urgency", "approval_state", "coverage_gap"]:
            t0 = time.time()
            after = v4.ask_h12_14_clarified(question, axis)
            elapsed = time.time() - t0
            print(f"\n  axis={axis} [{elapsed:.4f}s]")
            print("  " + after.answer.replace("\n", "\n  "))

        print(f"\n  rubric check (union across all 3 axis answers -- is everything golden-answers.md needs present *somewhere*):")
        combined = "\n".join(v4.ask_h12_14_clarified(question, a).answer for a in
                              ["review_urgency", "approval_state", "coverage_gap"])
        grade(combined, RUBRIC_ITEMS[tag])


if __name__ == "__main__":
    main()
