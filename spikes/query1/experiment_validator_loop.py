#!/usr/bin/env python3
"""Experiment: generator produces an answer, a validator reviews it against the
full tool-call trace (not just the answer text) and either passes it or sends
specific feedback back for revision, looping until PASS or a round cap.
Standalone -- deliberately not built into QueryMechanismV2, and duplicates a
slice of its agent-loop logic (with one difference: it keeps full tool RESULTS
alongside each call, which QueryMechanismV2.MechanismResult doesn't retain,
because the validator needs to check what was actually retrieved, not just
what was asked for).

Finding, from 2 runs against qwen3-coder-next:q4_K_M as both generator and
validator (same model, fresh context per call, no shared state):
- The validator itself worked: round 1 of run 1 produced a draft citing 5/7
  real obligations plus a false claim ("no CRA obligation names MFA"), and the
  validator correctly FAILed it with specific, accurate feedback rather than
  rubber-stamping. That's real evidence a second, independent pass can catch
  what a generating pass didn't self-catch.
- The revise-and-loop mechanism did not pay off in this sample: run 1's
  revision attempt never converged (ran out of its shared turn budget mid-
  revision, turning a mediocre-but-real 5/7 answer into no final answer at
  all); run 2's generator never converged even on the first pass. Feeding
  critique back into the SAME conversation, competing for the SAME turn
  budget as the original exploration, looks like the wrong design -- a
  revision attempt probably needs its own fresh budget, or a narrower
  instruction ("fetch this one specific missing thing") rather than "revise
  your whole answer."

See q-approach2.md's "Further experiments" section for the full discussion
and what a redesigned version might look like.
"""

import json
import time

from query_mechanism_v2 import GRAPH_SCHEMA, SYSTEM_PROMPT, TOOL_SPECS, OllamaClient, QueryMechanismV2, ToolBox
from experiment_self_consistency import QUESTION, REAL_OBLIGATIONS

# Original version: instructed what to check, but -- unlike the generator's
# SYSTEM_PROMPT -- never given the graph schema itself. Kept under this name
# (not renamed) so the original single-run finding in this module's docstring
# still refers to what was actually tested. See VALIDATOR_SYSTEM_GROUNDED
# below and q-approach2.md's "Was the validator cold-called?" section for why
# that gap mattered enough to test directly.
VALIDATOR_SYSTEM = """You are a strict validator for a compliance-graph question-answering agent. \
You will see: the original question, the full trace of tool calls the agent made (queries + \
raw rows returned), and the agent's draft final answer. Check three things:
1. Every distinct real entity id in the tool results that is relevant to the question is either \
cited in the answer, or its exclusion is explicitly justified. Silently dropping a retrieved, \
relevant row is a FAIL.
2. No claim in the answer lacks support in the trace (no fact stated that wasn't actually retrieved).
3. Any chain through Policy/Standard/Control states each one's status (approved/draft/deprecated, \
implemented/reviewed/planned/deprecated).
Respond with EXACTLY 'PASS' if all three hold, or 'FAIL: <specific, actionable feedback the agent \
can act on to fix the answer>' otherwise. Do not soften a real gap into a PASS."""

# Same checklist, but grounded in the identical GRAPH_SCHEMA text the generator
# gets -- the fix that eliminated the generator's property/direction failures,
# now tested on the validator's completeness judgment instead of Cypher-writing.
VALIDATOR_SYSTEM_GROUNDED = f"""You are a strict validator for a compliance-graph question-answering agent. \
You will see: the original question, the full trace of tool calls the agent made (queries + \
raw rows returned), and the agent's draft final answer.

{GRAPH_SCHEMA}

Check three things:
1. Every distinct real entity id in the tool results that is relevant to the question is either \
cited in the answer, or its exclusion is explicitly justified. Silently dropping a retrieved, \
relevant row is a FAIL. Use the schema above to recognize which ids in the trace are the \
relevant kind (e.g. Obligation ids when the question asks "which obligations") -- don't rely \
only on surface wording in the trace to decide relevance.
2. No claim in the answer lacks support in the trace (no fact stated that wasn't actually retrieved).
3. Any chain through Policy/Standard/Control states each one's status (approved/draft/deprecated, \
implemented/reviewed/planned/deprecated).
Respond with EXACTLY 'PASS' if all three hold, or 'FAIL: <specific, actionable feedback the agent \
can act on to fix the answer>' otherwise. Do not soften a real gap into a PASS."""


def _run_generator(client, question: str, max_turns: int, extra_user_msg: str | None = None):
    """Same turn-loop shape as QueryMechanismV2._ask_agent, but returns the full
    tool-call trace (calls AND results) alongside the answer, which the
    validator needs and MechanismResult doesn't carry.
    """
    tools = ToolBox(QueryMechanismV2().v1.graph)
    dispatch = {
        "list_entities": lambda a: tools.list_entities(a["label"]),
        "run_cypher": lambda a: tools.run_cypher(a["query"]),
        "whole_graph_stats": lambda a: tools.whole_graph_stats(),
    }
    messages: list[dict] = [{"role": "user", "content": question}]
    if extra_user_msg:
        messages.append({"role": "user", "content": extra_user_msg})
    trace_lines: list[str] = []
    for _ in range(max_turns):
        turn = client.complete(system=SYSTEM_PROMPT, messages=messages, tools=TOOL_SPECS)
        if not turn.tool_calls:
            return turn.text or "", trace_lines
        messages.append({"role": "assistant", "tool_calls": turn.tool_calls})
        for call in turn.tool_calls:
            try:
                result = dispatch[call.name](call.arguments)
            except Exception as exc:  # noqa: BLE001 -- surfaced in the trace, not swallowed
                result = {"error": str(exc)}
            trace_lines.append(f"CALL {call.name}({call.arguments}) -> {json.dumps(result, default=str)[:800]}")
            messages.append({"role": "tool", "tool_call_id": call.id, "content": result})
    return None, trace_lines


def critique_loop(model: str, question: str, max_rounds: int = 3, max_turns_per_round: int = 12):
    gen_client = OllamaClient(model=model)
    val_client = OllamaClient(model=model)  # self-validation: same model, independent call, no shared context
    extra_msg = None
    answer = None
    for round_n in range(1, max_rounds + 1):
        answer, trace = _run_generator(gen_client, question, max_turns_per_round, extra_user_msg=extra_msg)
        if answer is None:
            print(f"  round {round_n}: generator did not converge")
            return None, round_n
        val_prompt = f"QUESTION:\n{question}\n\nTOOL TRACE:\n" + "\n".join(trace) + f"\n\nDRAFT ANSWER:\n{answer}"
        val_turn = val_client.complete(system=VALIDATOR_SYSTEM, messages=[{"role": "user", "content": val_prompt}], tools=[])
        verdict = (val_turn.text or "").strip()
        cited = {oid for oid in REAL_OBLIGATIONS if oid in answer}
        print(f"  round {round_n}: cited {len(cited)}/7, validator: {verdict[:120]}")
        if verdict.upper().startswith("PASS"):
            return answer, round_n
        extra_msg = f"A validator reviewed your answer and found issues: {verdict}\nRevise your answer, making more tool calls if needed."
    return answer, max_rounds


def main(model: str = "qwen3-coder-next:q4_K_M", runs: int = 2) -> None:
    for i in range(runs):
        print(f"=== critique-loop run {i + 1} ===")
        t0 = time.time()
        answer, rounds = critique_loop(model, QUESTION)
        print(f"  elapsed {round(time.time() - t0, 1)}s, rounds used: {rounds}")
        if answer:
            cited = {oid for oid in REAL_OBLIGATIONS if oid in answer}
            print(f"  FINAL cited {len(cited)}/7")
        print()


if __name__ == "__main__":
    main()
