#!/usr/bin/env python3
"""Experiment: combine the two ideas that each showed partial promise on their
own. experiment_self_consistency.py found union-of-N's raw evidence beats any
single run; experiment_validator_loop.py found a validator correctly detects
real gaps but its auto-revise loop (feeding critique back into the SAME
generator conversation, competing for the SAME turn budget as the original
exploration) doesn't converge. This tests a third design that tries to get the
detection benefit without the loop's failure mode:

1. Run the generator N=3 independent times (no validation in between, no
   shared state between runs) -- same as self-consistency.
2. Pool every tool call + result from all 3 runs into one merged evidence set.
   This is strictly more evidence than any single run saw, since the 3 runs
   explored somewhat different paths.
3. ONE synthesis call, no tools available, asked to answer using only the
   pooled evidence -- forces it to draw from everything collectively
   discovered rather than re-exploring (which is what ate the revise loop's
   turn budget last time).
4. ONE validation call against that synthesized answer.

Finding: it did NOT outperform plain self-consistency, and revealed a new
problem. One real run: generator runs produced 0/7, 7/7, and a non-
convergent 0/7 -- pooling their answer-level citations (i.e. just the
set-union experiment_self_consistency.py already does) reaches 7/7, same
as before. But the synthesis call, despite having all 28 pooled tool-call
results available -- including the exact evidence that let run 2 reach
7/7 on its own -- only extracted 4/7 into its answer. And the validator,
which correctly caught a real 5/7 gap in experiment_validator_loop.py's
isolated test, PASSed this 4/7 answer without complaint. Two independent
slippages in the same run: an LLM synthesis step lost information that was
mechanically present in its context, and the validator wasn't reliably
strict the second time it was asked to do the same job. Net effect: this
three-call combination (3 generators + synthesis + validation) produced a
*worse*, falsely-validated answer than simply extracting each run's cited
ids with a regex and taking the union, which needs zero extra LLM calls.
The lesson generalizes past this one experiment: pooling raw evidence
helps, but handing the last mile (turning pooled evidence into one
answer) to more LLM judgment doesn't reliably realize that benefit --
consistent with this whole spike's recurring theme (`_annotate_trust()` in
query_mechanism_v1.py, the trust-flag discipline throughout) that
structural/mechanical combination beats prose synthesis wherever it's
possible to avoid the latter. Standalone, not wired into
query_mechanism_v2.py -- see q-approach2.md's "Further experiments"
section for the full writeup.
"""

import time

from query_mechanism_v2 import OllamaClient
from experiment_self_consistency import QUESTION, REAL_OBLIGATIONS
from experiment_validator_loop import VALIDATOR_SYSTEM, _run_generator

SYNTHESIS_SYSTEM = """You answer questions about the Policy System compliance graph using ONLY \
the evidence given below -- it was gathered by multiple independent exploration attempts, so it \
may contain overlapping or redundant tool calls; treat it as one pooled evidence set, not several \
separate answers to reconcile. You have no tools in this step and cannot query anything further.

Rules, non-negotiable:
1. Never state a fact not present in the evidence given.
2. Your answer must account for every distinct real entity id in the evidence that's relevant \
to the question -- if you exclude one, say why. Dropping a relevant row present in the evidence \
is treated as a wrong answer.
3. Whenever a chain passes through Policy, Standard, or Control, state each one's status -- a \
chain through a deprecated Policy or planned Control is not current evidence.
4. If the pooled evidence doesn't answer the question, say so plainly rather than guessing.
"""


def main(model: str = "qwen3-coder-next:q4_K_M", n_runs: int = 3, max_turns: int = 12) -> None:
    import query_mechanism_v2 as v2

    v2.MAX_AGENT_TURNS = max_turns
    gen_client = OllamaClient(model=model)

    pooled_trace: list[str] = []
    per_run_cited = []
    t0 = time.time()
    for i in range(n_runs):
        answer, trace = _run_generator(gen_client, QUESTION, max_turns)
        cited = {oid for oid in REAL_OBLIGATIONS if answer and oid in answer}
        per_run_cited.append(cited)
        pooled_trace.extend(trace)
        print(f"generator run {i + 1}: {'converged' if answer is not None else 'DID NOT CONVERGE'}, "
              f"{len(trace)} tool calls this run, cited {len(cited)}/7")

    print(f"\npooled evidence: {len(pooled_trace)} tool-call results across {n_runs} runs "
          f"(union of individual runs' citations: {len(set().union(*per_run_cited))}/7)")

    synth_client = OllamaClient(model=model)
    synth_prompt = f"QUESTION:\n{QUESTION}\n\nPOOLED EVIDENCE:\n" + "\n".join(pooled_trace)
    synth_turn = synth_client.complete(system=SYNTHESIS_SYSTEM, messages=[{"role": "user", "content": synth_prompt}], tools=[])
    synthesized = synth_turn.text or ""
    synth_cited = {oid for oid in REAL_OBLIGATIONS if oid in synthesized}
    print(f"\nsynthesized answer cited {len(synth_cited)}/7: {sorted(synth_cited)}")

    val_client = OllamaClient(model=model)
    val_prompt = f"QUESTION:\n{QUESTION}\n\nTOOL TRACE:\n" + "\n".join(pooled_trace) + f"\n\nDRAFT ANSWER:\n{synthesized}"
    val_turn = val_client.complete(system=VALIDATOR_SYSTEM, messages=[{"role": "user", "content": val_prompt}], tools=[])
    verdict = (val_turn.text or "").strip()
    print(f"validator verdict: {verdict}")

    print(f"\ntotal elapsed: {round(time.time() - t0, 1)}s ({n_runs} generator runs + synthesis + validation, "
          f"no revise loop)")


if __name__ == "__main__":
    main()
