#!/usr/bin/env python3
"""Experiment: does a deterministic post-check -- "does the final answer's text
reference every distinct primary key from the last tool result?" -- catch the
stopping-early/under-citing failures left over after the grounding fix and the
direction corrector (direction-correction.md)? Scoped exactly per q-approach2.md's
"Next" section item 3, and tested the same way that doc's other three combination
ideas were tested before any adoption call (experiment_self_consistency.py,
experiment_temperature.py, experiment_validator_loop.py,
experiment_union_plus_validator.py): standalone script, not wired into
query_mechanism_v2.py, real evidence before a recommendation.

Two-phase test:
1. Deterministic self-tests against hand-built fixtures using REAL ids pulled from
   golden-answers.md (H11's 7-obligation set) -- no LLM, no graph, just proving the
   extraction/comparison logic itself is correct before trusting it against live
   output.
2. Live re-run of the same 7 (question, model) trials
   experiment_direction_correction_rerun.py already re-ran (H9, H1, M14, H13, H11 on
   qwen3:14b; H1, H11 on qwen3-coder-next:q4_K_M), logging the FULL tool-call
   history (not just run_cypher, per that script's narrower log) so the check can be
   evaluated under three scoping variants:
     - naive: last tool call of ANY kind (including whole_graph_stats/list_entities)
     - run_cypher-only: last run_cypher call specifically
     - run_cypher-union: every distinct id across ALL run_cypher calls in the trace
   The naive variant is expected to misfire on whole_graph_stats-routed questions
   (H13/H12/H14 shape) -- its response is a hand-picked aggregate, not a row set the
   model is obligated to cite in full (SYSTEM_PROMPT rule 6 is scoped to "when a
   query returns multiple rows," i.e. run_cypher, not the pre-computed aggregate
   tool). Included deliberately to make that failure mode visible rather than
   silently scoping around it upfront.
"""

import time
from typing import Any

from query_mechanism_v2 import OllamaClient, QueryMechanismV2, extract_entity_ids

# --------------------------------------------------------------------------
# Phase 1: the check itself
# --------------------------------------------------------------------------
#
# extract_entity_ids now lives in query_mechanism_v2.py -- this experiment's
# rejected standalone-gate design ended up adopted for a narrower job instead
# (scoring union-of-N runs against each other, see q-approach2.md's "Citation
# completeness as a deterministic post-check" section for why the standalone
# gate itself wasn't adopted).


def check_citation_completeness(source: Any, answer: str) -> dict:
    """`source` is whatever tool result(s) the caller decides should have been
    fully cited (a single tool result dict, or a manually unioned set of ids from
    several). Returns total/cited/missing so callers can report evidence, not
    just a boolean -- same "surface what changed, don't hide it" instinct as
    `run_cypher`'s `direction_corrected` key.
    """
    ids = extract_entity_ids(source)
    missing = {i for i in ids if i not in answer}
    return {"total": ids, "cited": ids - missing, "missing": missing}


def _self_test() -> None:
    """Deterministic, no LLM/graph -- proves the extraction regex and the
    completeness comparison are correct in isolation, using REAL ids from
    golden-answers.md's H11 entry, before trusting this against live output.
    """
    h11_obligations = {
        "obl_protect_against_unauthorised_access_ef908f",
        "obl_ensure_confidentiality_integrity_availability_and_resilience_of_p_888591",
        "obl_ensure_confidentiality_integrity_availability_and_resilience_of_p_408068",
        "obl_maintain_human_resources_security_access_control_and_asset_m_644c45",
        "obl_maintain_human_resources_security_access_control_and_asset_m_40eba8",
        "obl_deploy_multi_factor_authentication_and_secured_communication_138a1f",
        "obl_deploy_multi_factor_authentication_and_secured_communication_c2a8ea",
    }
    fake_run_cypher_result = {
        "columns": ["o.id", "o.text"],
        "rows": [
            [oid, "some obligation text unrelated to the id itself"]
            for oid in sorted(h11_obligations)
        ],
    }

    # 1. Full citation -> nothing missing.
    full_answer = "The affected obligations are: " + ", ".join(sorted(h11_obligations)) + "."
    r = check_citation_completeness(fake_run_cypher_result, full_answer)
    assert r["missing"] == set(), f"expected no missing ids, got {r['missing']}"
    assert r["total"] == h11_obligations, f"expected exactly the 7 real ids, got {r['total']}"

    # 2. Real observed failure shape: 3 of 7 cited (q-approach2.md's actual finding).
    three_cited = sorted(h11_obligations)[:3]
    partial_answer = "The affected obligations include: " + ", ".join(three_cited) + "."
    r = check_citation_completeness(fake_run_cypher_result, partial_answer)
    assert len(r["missing"]) == 4, f"expected 4 missing (7 - 3 cited), got {len(r['missing'])}: {r['missing']}"
    assert r["cited"] == set(three_cited)

    # 3. Zero citation -> everything missing, not a crash.
    vague_answer = "Several obligations across all three regulations would be affected."
    r = check_citation_completeness(fake_run_cypher_result, vague_answer)
    assert r["missing"] == h11_obligations

    # 4. No ids in the source at all (e.g. a count-only row) -> nothing to check,
    # not a false "missing" flag.
    count_only_result = {"columns": ["count(o)"], "rows": [[7]]}
    r = check_citation_completeness(count_only_result, "There are 7 affected obligations.")
    assert r["total"] == set() and r["missing"] == set()

    # 5. Regulation/Requirement id shapes extract correctly and distinctly from
    # the underscore-prefixed family (H1's real requirement ids).
    h1_reqs = {"GDPR-1.0_req_art_32.1", "GDPR-1.0_req_art_32.1a", "GDPR-1.0_req_art_32.1d", "GDPR-1.0_req_art_32.4"}
    h1_result = {"columns": ["req.id"], "rows": [[r] for r in sorted(h1_reqs)]}
    r = check_citation_completeness(h1_result, "Covers " + ", ".join(sorted(h1_reqs)))
    assert r["total"] == h1_reqs, f"requirement-id extraction mismatch: {r['total']}"
    assert r["missing"] == set()

    # 6. The false-positive risk this experiment exists to surface: a legitimate,
    # rubric-correct H13-style narrative answer, checked against a
    # whole_graph_stats-shaped result carrying dozens of incidental ids the
    # rubric explicitly does NOT require citing individually (H13's rubric wants
    # grounded *counts*, not a roll call of every overdue control's id).
    stats_like = {
        "capabilities_total": 68,
        "capabilities_ungoverned": 55,
        "controls_overdue_for_review": [
            {"id": "ctrl_std_pol_incident_vulnerability_response_policy_9de859_v1_automated",
             "title": "Quarterly Incident Triage SLA Review", "next_review_date": "2026-07-20"}
        ],
        "controls_planned_not_yet_implemented": [
            {"id": "ctrl_std_pol_incident_vulnerability_response_policy_9de859_v2_automated",
             "title": "Automated Vulnerability Patch SLA Check"}
        ],
    }
    legit_h13_answer = (
        "Of 68 capabilities, 13 are governed and 55 remain ungoverned. One control is "
        "overdue for review and one more is still planned rather than implemented."
    )
    r = check_citation_completeness(stats_like, legit_h13_answer)
    assert len(r["missing"]) == 2, (
        f"expected the naive check to flag both control ids as 'missing' from a rubric-correct "
        f"answer -- that's the false-positive this experiment is testing for, got {r['missing']}"
    )

    print("self-test: 6/6 checks passed (extraction + comparison logic verified against real ids)")


# --------------------------------------------------------------------------
# Phase 2: live re-run, same 7 trials as experiment_direction_correction_rerun.py
# --------------------------------------------------------------------------

QUESTIONS = {
    "H9": "Our security scanner flagged missing rate-limiting on an endpoint that "
    "processes health data — does that block a GDPR-relevant control?",
    "H1": "Are we compliant with GDPR Article 32?",
    "M14": "Which of our draft Policies are blocking GDPR readiness?",
    "H13": "Give me a one-paragraph summary of our overall compliance posture I can bring to the board.",
    "H11": "If an attacker exploited a missing MFA control today, which regulatory "
    "obligations across CRA/NIS2/GDPR would we be out of compliance with?",
}

# Same baseline this experiment diffs against as experiment_direction_correction_rerun.py
# used -- quoted from q-approach2.md's "Result" table plus direction-correction.md's
# empirical re-run section, not re-derived here.
PRIOR_VERDICT = {
    ("H9", "qwen3:14b"): "partial (lucky exact-match guess, correct conclusion)",
    ("H1", "qwen3:14b"): "fail x2 (wrong property names, then wrong ID-pattern -- never reached a multi-row citation point)",
    ("M14", "qwen3:14b"): "pass (after schema-grounding fix)",
    ("H13", "qwen3:14b"): "partial (hallucinated a number already in hand -- synthesis, not a dropped-id problem)",
    ("H11", "qwen3:14b"): "partial then fail across earlier runs; direction-correction.md's re-run: detailed, mostly-accurate",
    ("H1", "qwen3-coder-next:q4_K_M"): "direction-correction.md re-run: partial, correctly citing real Standards/Controls, still incomplete against rubric (2 ungoverned sub-clauses never queried for)",
    ("H11", "qwen3-coder-next:q4_K_M"): "direction-correction.md re-run: converged in 3 tool calls, found only 1 of 7 real obligations, said so honestly (classic under-citing, unresolved)",
}


def run_one(qid: str, question: str, model: str, max_turns: int) -> dict:
    import query_mechanism_v2 as v2

    v2.MAX_AGENT_TURNS = max_turns
    mech = QueryMechanismV2(llm=OllamaClient(model=model))

    # Wrap every tool dispatch (not just run_cypher) -- unlike
    # experiment_direction_correction_rerun.py, this needs the FULL trace
    # (list_entities/whole_graph_stats included) to test the naive "last tool
    # call, whatever it was" variant honestly.
    history: list[tuple[str, dict, Any]] = []
    original_dispatch = dict(mech._dispatch)

    def _wrap(name, fn):
        def wrapped(args):
            result = fn(args)
            history.append((name, args, result))
            return result
        return wrapped

    mech._dispatch = {name: _wrap(name, fn) for name, fn in original_dispatch.items()}

    t0 = time.time()
    outcome: dict = {"qid": qid, "model": model, "elapsed": None, "error": None, "answer": None, "history": history}
    try:
        result = mech.ask(question)
        outcome["elapsed"] = round(time.time() - t0, 1)
        outcome["answer"] = result.answer
        outcome["tool_calls"] = len(result.tool_calls_made)
    except Exception as exc:  # noqa: BLE001 -- reported, not swallowed
        outcome["elapsed"] = round(time.time() - t0, 1)
        outcome["error"] = str(exc)
    return outcome


def _report(outcome: dict) -> None:
    qid, model = outcome["qid"], outcome["model"]
    prior = PRIOR_VERDICT.get((qid, model), "(not previously tested)")
    history = outcome["history"]
    print(f"\n=== {qid} / {model} ({outcome['elapsed']}s) ===")
    print(f"prior verdict:  {prior}")
    call_summary = ", ".join(name for name, _, _ in history) or "(no tool calls)"
    print(f"tool call sequence: {call_summary}")

    if outcome["error"]:
        print(f"DID NOT CONVERGE / ERROR: {outcome['error']}")
        return

    answer = outcome["answer"]
    print(f"answer:\n{answer}")

    run_cypher_calls = [(args, res) for name, args, res in history if name == "run_cypher"]
    last_call = history[-1] if history else None

    # Variant: naive -- last tool call of any kind.
    if last_call is not None:
        naive = check_citation_completeness(last_call[2], answer)
        print(
            f"[naive: last tool call was {last_call[0]!r}] "
            f"{len(naive['cited'])}/{len(naive['total'])} ids cited"
            + (f", missing: {sorted(naive['missing'])}" if naive["missing"] else "")
        )
    else:
        print("[naive] no tool calls at all -- nothing to check")

    # Variant: run_cypher-only, last one.
    if run_cypher_calls:
        last_rc_result = run_cypher_calls[-1][1]
        rc_last = check_citation_completeness(last_rc_result, answer)
        print(
            f"[run_cypher-only, last of {len(run_cypher_calls)}] "
            f"{len(rc_last['cited'])}/{len(rc_last['total'])} ids cited"
            + (f", missing: {sorted(rc_last['missing'])}" if rc_last["missing"] else "")
        )

        # Variant: run_cypher-union across the whole trace.
        union_ids: set[str] = set()
        for _, res in run_cypher_calls:
            union_ids |= extract_entity_ids(res)
        rc_union = check_citation_completeness(union_ids, answer)
        print(
            f"[run_cypher-union across {len(run_cypher_calls)} calls] "
            f"{len(rc_union['cited'])}/{len(rc_union['total'])} ids cited"
            + (f", missing: {sorted(rc_union['missing'])}" if rc_union["missing"] else "")
        )
    else:
        print("[run_cypher-only / run_cypher-union] no run_cypher calls in this trace (routed through "
              "list_entities/whole_graph_stats only) -- these variants have nothing to check")


def main() -> None:
    _self_test()

    t0 = time.time()
    trials = [(qid, "qwen3:14b", 8) for qid in ["H9", "H1", "M14", "H13", "H11"]]
    trials += [(qid, "qwen3-coder-next:q4_K_M", 16) for qid in ["H1", "H11"]]

    for qid, model, max_turns in trials:
        outcome = run_one(qid, QUESTIONS[qid], model, max_turns)
        _report(outcome)

    print(f"\ntotal elapsed: {round(time.time() - t0, 1)}s")


if __name__ == "__main__":
    main()
