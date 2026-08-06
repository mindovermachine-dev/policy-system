#!/usr/bin/env python3
"""Experiment: was the validator's false PASS in experiment_union_plus_validator.py
explained by the same root cause the generator's failures had -- no graph schema
in its system prompt, just a checklist? The generator's SYSTEM_PROMPT (in
query_mechanism_v2.py) includes GRAPH_SCHEMA; VALIDATOR_SYSTEM (in
experiment_validator_loop.py) never did.

Controlled test, not a fresh independent run: generate ONE pooled-evidence
synthesis (3 generator runs + 1 synthesis call, same recipe as
experiment_union_plus_validator.py), then validate that SAME trace + SAME
draft answer TWICE -- once with the original VALIDATOR_SYSTEM, once with
VALIDATOR_SYSTEM_GROUNDED (identical checklist, GRAPH_SCHEMA added). Only the
validator's prompt changes between the two calls; the input is held fixed, so
any difference in verdict isolates that one variable rather than being
confounded by a fresh, differently-variable generator run.

See q-approach2.md's "Was the validator cold-called?" section for the result
and what it does/doesn't establish (a single controlled comparison, not a
statistically powered study).
"""

import time

from query_mechanism_v2 import OllamaClient
from experiment_self_consistency import QUESTION, REAL_OBLIGATIONS
from experiment_union_plus_validator import SYNTHESIS_SYSTEM
from experiment_validator_loop import VALIDATOR_SYSTEM, VALIDATOR_SYSTEM_GROUNDED, _run_generator


def main(model: str = "qwen3-coder-next:q4_K_M", n_runs: int = 3, max_turns: int = 12) -> None:
    import query_mechanism_v2 as v2

    v2.MAX_AGENT_TURNS = max_turns
    gen_client = OllamaClient(model=model)

    pooled_trace: list[str] = []
    t0 = time.time()
    for i in range(n_runs):
        answer, trace = _run_generator(gen_client, QUESTION, max_turns)
        pooled_trace.extend(trace)
        cited = {oid for oid in REAL_OBLIGATIONS if answer and oid in answer}
        print(f"generator run {i + 1}: {'converged' if answer is not None else 'DID NOT CONVERGE'}, "
              f"{len(trace)} tool calls, cited {len(cited)}/7")

    synth_client = OllamaClient(model=model)
    synth_prompt = f"QUESTION:\n{QUESTION}\n\nPOOLED EVIDENCE:\n" + "\n".join(pooled_trace)
    synth_turn = synth_client.complete(system=SYNTHESIS_SYSTEM, messages=[{"role": "user", "content": synth_prompt}], tools=[])
    synthesized = synth_turn.text or ""
    synth_cited = {oid for oid in REAL_OBLIGATIONS if oid in synthesized}
    print(f"\nsynthesized answer cited {len(synth_cited)}/7 -- held fixed for both validator calls below")

    val_prompt = f"QUESTION:\n{QUESTION}\n\nTOOL TRACE:\n" + "\n".join(pooled_trace) + f"\n\nDRAFT ANSWER:\n{synthesized}"

    ungrounded_client = OllamaClient(model=model)
    ungrounded_turn = ungrounded_client.complete(system=VALIDATOR_SYSTEM, messages=[{"role": "user", "content": val_prompt}], tools=[])
    print(f"\n[ungrounded VALIDATOR_SYSTEM] verdict: {(ungrounded_turn.text or '').strip()}")

    grounded_client = OllamaClient(model=model)
    grounded_turn = grounded_client.complete(system=VALIDATOR_SYSTEM_GROUNDED, messages=[{"role": "user", "content": val_prompt}], tools=[])
    print(f"\n[VALIDATOR_SYSTEM_GROUNDED] verdict: {(grounded_turn.text or '').strip()}")

    print(f"\ntotal elapsed: {round(time.time() - t0, 1)}s")


if __name__ == "__main__":
    main()
