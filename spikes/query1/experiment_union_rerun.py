#!/usr/bin/env python3
"""Empirical re-verification: does union-of-N, now wired into
`QueryMechanismV2._ask_agent_union` (see q-approach2.md's "Further
experiments" section for the evidence behind it), actually move real-model
outcomes on the specific questions/models where under-citing was previously
documented -- not just prove the combination logic is correct in isolation
(already covered by test_query_mechanism_v2.py's scripted union tests).

Same empirical-verification pattern as experiment_direction_correction_rerun.py
used for the direction corrector: rerun real questions through the live agent
loop with the fix now active, and diff against the previously recorded
single-run verdict so any change can be attributed rather than assumed.

Scope: H1, H11 (both real-model, both models where under-citing/wrong-claim
failures were previously documented -- see PRIOR_FINDING below, sourced from
direction-correction.md's re-run and experiment_citation_completeness.py's
own re-run) and H13 (qwen3:14b only -- global/whole_graph_stats question, the
synthesis-hallucination case union-of-N is NOT expected to fix, included so
that negative result is also evidenced rather than assumed).
"""

import time

from query_mechanism_v2 import OllamaClient, QueryMechanismV2

QUESTIONS = {
    "H1": "Are we compliant with GDPR Article 32?",
    "H13": "Give me a one-paragraph summary of our overall compliance posture I can bring to the board.",
    "H11": "If an attacker exploited a missing MFA control today, which regulatory "
    "obligations across CRA/NIS2/GDPR would we be out of compliance with?",
}

# The specific, concrete single-run defects this rerun checks whether
# union-of-3 fixes -- quoted from direction-correction.md's empirical re-run
# and experiment_citation_completeness.py's own live re-run (this doc's
# "Citation completeness as a deterministic post-check" section in
# q-approach2.md), not re-derived here.
PRIOR_FINDING = {
    ("H1", "qwen3:14b"): "never mentioned the GDPR-1.0_req_art_32.1 umbrella clause at all (golden-answers.md's own documented recurring miss)",
    ("H13", "qwen3:14b"): "hallucinated a specific number ('two automated controls unimplemented' vs. real count of 1) -- NOT an id-citation gap, union-of-N is not expected to touch this",
    ("H11", "qwen3:14b"): "single run returned 0 rows from a wrong property/mapping query and answered 'no such obligations modeled' -- wrong before any evidence existed to combine",
    ("H1", "qwen3-coder-next:q4_K_M"): "correctly NAMED cap_cybersecurity_risk_management_program_50601b and cap_security_control_effectiveness_assessment_627623 but FALSELY claimed both are governed by approved policies (golden: both entirely ungoverned) -- a wrong claim about a cited id, not a dropped one",
    ("H11", "qwen3-coder-next:q4_K_M"): "cited 5 of 7 real obligations, silently dropped the 2 NIS2 HR-security ones (obl_maintain_human_resources_security_access_control_and_asset_m_644c45 / ..._40eba8) despite having retrieved them earlier in a 9-call trace",
}


def run_one(qid: str, question: str, model: str, max_turns: int) -> None:
    import query_mechanism_v2 as v2

    v2.MAX_AGENT_TURNS = max_turns
    mech = QueryMechanismV2(llm=OllamaClient(model=model))  # union_runs defaults to UNION_RUNS_DEFAULT (3)

    t0 = time.time()
    prior = PRIOR_FINDING.get((qid, model), "(not previously tested)")
    try:
        result = mech.ask(question)
        elapsed = round(time.time() - t0, 1)
        print(f"\n=== {qid} / {model} ({elapsed}s, {result.runs_sampled}/{mech.union_runs} runs converged, "
              f"{len(result.tool_calls_made)} total tool calls) ===")
        print(f"prior single-run finding: {prior}")
        print(f"ids added by union (found in a non-primary run, not the chosen answer): {result.union_ids_added or '(none)'}")
        print(f"answer:\n{result.answer}")
    except Exception as exc:  # noqa: BLE001 -- reported, not swallowed
        elapsed = round(time.time() - t0, 1)
        print(f"\n=== {qid} / {model} ({elapsed}s) === FAILED: {exc}")
        print(f"prior single-run finding: {prior}")


def main() -> None:
    t0 = time.time()

    for qid in ["H1", "H13", "H11"]:
        run_one(qid, QUESTIONS[qid], "qwen3:14b", max_turns=8)

    for qid in ["H1", "H11"]:
        run_one(qid, QUESTIONS[qid], "qwen3-coder-next:q4_K_M", max_turns=16)

    print(f"\ntotal elapsed: {round(time.time() - t0, 1)}s")


if __name__ == "__main__":
    main()
