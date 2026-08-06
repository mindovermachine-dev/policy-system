#!/usr/bin/env python3
"""Experiment: does the deterministic direction corrector (direction-correction.md)
actually move real-model outcomes, or did we just prove the string-rewrite logic
works in isolation? Re-runs the exact 5 questions q-approach2.md's "Result" table
already has real-model verdicts for -- H9, H1, M14, H13, H11 -- through the SAME
QueryMechanismV2 agent loop, same models, with the corrector now live in
ToolBox.run_cypher, and diffs the outcome against the prior recorded verdict.

Only H1 and H11 are re-run against qwen3-coder-next:q4_K_M too -- that's the
specific (question, model) pairing where direction-reversal was actually observed
(q-approach2.md rows: "(pol)<-[:SUPPORTED_BY]-(:Standard)" on H1, repeated
"(reg:Regulation)<-[:DEFINES]-(role:Role)" on H11, the latter never converging even
at a raised 16-turn cap). qwen3:14b's prior failures on those same two questions
were property-name/ID-pattern mistakes that happened *before* the chain ever
reached the hop that gets reversed -- re-running them isn't expected to surface
this particular fix, but is included anyway for a same-model, same-turn-budget
baseline comparison.

`ToolBox.run_cypher` is wrapped (not modified) to log every `direction_corrected`
firing, so this script can report not just pass/fail but *whether the corrector is
what changed the outcome* -- the actual question the user asked: how much of the
observed failure population does this fix reach, empirically, not just in the
unit-test string-rewrite sense already covered by test_query_mechanism_v2.py.
"""

import time

from query_mechanism_v2 import OllamaClient, QueryMechanismV2

QUESTIONS = {
    "H9": "Our security scanner flagged missing rate-limiting on an endpoint that "
    "processes health data — does that block a GDPR-relevant control?",
    "H1": "Are we compliant with GDPR Article 32?",
    "M14": "Which of our draft Policies are blocking GDPR readiness?",
    "H13": "Give me a one-paragraph summary of our overall compliance posture I can bring to the board.",
    "H11": "If an attacker exploited a missing MFA control today, which regulatory "
    "obligations across CRA/NIS2/GDPR would we be out of compliance with?",
}

# Prior real-model verdict from q-approach2.md's "Result" table, kept here only
# as the baseline this script diffs against -- not re-derived, just quoted.
PRIOR_VERDICT = {
    ("H9", "qwen3:14b"): "partial (lucky exact-match guess, correct conclusion)",
    ("H1", "qwen3:14b"): "fail x2 (wrong property names, then wrong ID-pattern -- never reached the reversed hop)",
    ("M14", "qwen3:14b"): "pass (after schema-grounding fix)",
    ("H13", "qwen3:14b"): "partial (hallucinated a number already in hand -- synthesis, not retrieval)",
    ("H11", "qwen3:14b"): "partial then fail (dropped rows, then wrong id/name filter -- never reached the reversed hop)",
    ("H1", "qwen3-coder-next:q4_K_M"): "partial, non-compliant (WRONG) -- direction reversal zeroed real evidence",
    ("H11", "qwen3-coder-next:q4_K_M"): "fail -- never converged, direction reversal repeated, exceeded 16-turn cap",
}


def run_one(qid: str, question: str, model: str, max_turns: int) -> None:
    import query_mechanism_v2 as v2

    v2.MAX_AGENT_TURNS = max_turns
    mech = QueryMechanismV2(llm=OllamaClient(model=model))

    corrections_log: list[tuple[str, list[str]]] = []
    original_run_cypher = mech.tools.run_cypher

    def logged_run_cypher(query: str) -> dict:
        result = original_run_cypher(query)
        if "direction_corrected" in result:
            corrections_log.append((query, result["direction_corrected"]))
        return result

    mech.tools.run_cypher = logged_run_cypher

    t0 = time.time()
    try:
        result = mech.ask(question)
        elapsed = round(time.time() - t0, 1)
        prior = PRIOR_VERDICT.get((qid, model), "(not previously tested)")
        print(f"\n=== {qid} / {model} ({elapsed}s, {len(result.tool_calls_made)} tool calls) ===")
        print(f"prior verdict:  {prior}")
        print(f"direction corrections fired: {len(corrections_log)}")
        for query, notes in corrections_log:
            print(f"  - {notes}")
        print(f"answer:\n{result.answer}")
    except Exception as exc:  # noqa: BLE001 -- surfaced in the report, not swallowed
        elapsed = round(time.time() - t0, 1)
        prior = PRIOR_VERDICT.get((qid, model), "(not previously tested)")
        print(f"\n=== {qid} / {model} ({elapsed}s) === DID NOT CONVERGE / ERROR: {exc}")
        print(f"prior verdict:  {prior}")
        print(f"direction corrections fired: {len(corrections_log)}")
        for query, notes in corrections_log:
            print(f"  - {notes}")


def main() -> None:
    t0 = time.time()

    for qid in ["H9", "H1", "M14", "H13", "H11"]:
        run_one(qid, QUESTIONS[qid], "qwen3:14b", max_turns=8)

    for qid in ["H1", "H11"]:
        run_one(qid, QUESTIONS[qid], "qwen3-coder-next:q4_K_M", max_turns=16)

    print(f"\ntotal elapsed: {round(time.time() - t0, 1)}s")


if __name__ == "__main__":
    main()
