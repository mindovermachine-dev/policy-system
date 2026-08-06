#!/usr/bin/env python3
"""Experiment: does sampling QueryMechanismV2's agent loop N times and combining
the results improve on a single run? Deliberately standalone -- NOT wired into
query_mechanism_v2.py or QueryMechanismV2 itself (see q-approach2.md's "Further
experiments" section for why: the combination strategy that actually works here
turned out to matter more than expected, and wasn't obvious going in).

Uses H11 (the MFA backward multi-hop question) because it has an objective,
checkable completeness metric -- 7 real obligation ids, confirmed independently
against the live graph -- rather than a rubric that needs human judgment.

Finding: union across runs reached 7/7; strict intersection (majority/consensus)
reached 0/7, because one of three runs produced no answer at all (turn-limit
timeout) and the two that did answer disagreed on how much to include. Classic
self-consistency assumes most samples cluster near a right answer and outliers
get voted out -- that assumption didn't hold here. See q-approach2.md for the
full writeup and what this implies for a future best-of-N design.
"""

import time

from query_mechanism_v2 import AgentTurnLimitExceeded, OllamaClient, QueryMechanismV2

REAL_OBLIGATIONS = {
    "obl_protect_against_unauthorised_access_ef908f",
    "obl_ensure_confidentiality_integrity_availability_and_resilience_of_p_888591",
    "obl_ensure_confidentiality_integrity_availability_and_resilience_of_p_408068",
    "obl_maintain_human_resources_security_access_control_and_asset_m_644c45",
    "obl_maintain_human_resources_security_access_control_and_asset_m_40eba8",
    "obl_deploy_multi_factor_authentication_and_secured_communication_138a1f",
    "obl_deploy_multi_factor_authentication_and_secured_communication_c2a8ea",
}

QUESTION = (
    "If an attacker exploited a missing MFA control today, which regulatory "
    "obligations across CRA/NIS2/GDPR would we be out of compliance with?"
)


def main(model: str = "qwen3-coder-next:q4_K_M", runs: int = 3, max_turns: int = 12) -> None:
    import query_mechanism_v2 as v2

    v2.MAX_AGENT_TURNS = max_turns  # default (8) is too tight for this model's more thorough style

    all_cited: list[set[str]] = []
    for i in range(runs):
        mech = QueryMechanismV2(llm=OllamaClient(model=model))
        t0 = time.time()
        try:
            r = mech.ask(QUESTION)
            cited = {oid for oid in REAL_OBLIGATIONS if oid in r.answer}
            all_cited.append(cited)
            print(f"run {i + 1}: {round(time.time() - t0, 1)}s, {len(r.tool_calls_made)} tool calls, "
                  f"cited {len(cited)}/7: {sorted(cited)}")
        except AgentTurnLimitExceeded:
            all_cited.append(set())
            print(f"run {i + 1}: {round(time.time() - t0, 1)}s, DID NOT CONVERGE within {max_turns} turns")

    union = set().union(*all_cited) if all_cited else set()
    intersection = set.intersection(*all_cited) if all_cited else set()
    print()
    print(f"union across {runs} runs: {len(union)}/7")
    print(f"intersection (strict consensus): {len(intersection)}/7")


if __name__ == "__main__":
    main()
