#!/usr/bin/env python3
"""Experiment: does lowering sampling temperature reduce the run-to-run variance
seen in experiment_self_consistency.py? Naive expectation: yes, lower temperature
means more deterministic output, fewer wrong turns. Standalone, not wired into
query_mechanism_v2.py -- OllamaClient doesn't expose a temperature parameter at
all; this defines a thin subclass just for the experiment.

Finding: the opposite. Default (Ollama's unset temperature) got 7/7, 3/7, then a
timeout across 3 runs. temperature=0.1 got 7/7 then two timeouts. temperature=0.0
got three timeouts, zero successful answers. Lower temperature made outcomes
worse in this small sample, consistent with a known failure mode of near-greedy
decoding on multi-step agentic tasks: it can get stuck cycling through slight
variations of the same unproductive query instead of the randomness needed to
jump to a different approach when stuck. See q-approach2.md for the full table
and caveats (n=3 per setting -- a real signal, not a statistically large one).
"""

import json
import time

import query_mechanism_v2 as v2
from query_mechanism_v2 import AgentTurnLimitExceeded, LLMTurn, QueryMechanismV2, ToolCall
from experiment_self_consistency import QUESTION, REAL_OBLIGATIONS


class TempOllamaClient(v2.OllamaClient):
    """OllamaClient with an explicit temperature -- not in the shipped class
    because query_mechanism_v2.py's default behavior shouldn't change based on
    an experiment that (per the finding above) argues against touching it.
    """

    def __init__(self, model: str, temperature: float):
        super().__init__(model=model)
        self.temperature = temperature

    def complete(self, system: str, messages: list[dict], tools: list) -> LLMTurn:
        oa_messages = [{"role": "system", "content": system}]
        for m in messages:
            if m["role"] == "user":
                oa_messages.append({"role": "user", "content": m["content"]})
            elif m["role"] == "assistant":
                oa_messages.append(
                    {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {"id": tc.id, "type": "function", "function": {"name": tc.name, "arguments": json.dumps(tc.arguments)}}
                            for tc in m["tool_calls"]
                        ],
                    }
                )
            elif m["role"] == "tool":
                oa_messages.append({"role": "tool", "tool_call_id": m["tool_call_id"], "content": json.dumps(m["content"], default=str)})

        oa_tools = [{"type": "function", "function": {"name": t.name, "description": t.description, "parameters": t.input_schema}} for t in tools]
        resp = self._client.chat.completions.create(model=self.model, messages=oa_messages, tools=oa_tools, temperature=self.temperature)
        msg = resp.choices[0].message
        if msg.tool_calls:
            calls = [ToolCall(id=tc.id, name=tc.function.name, arguments=json.loads(tc.function.arguments)) for tc in msg.tool_calls]
            return LLMTurn(tool_calls=calls)
        return LLMTurn(text=msg.content or "")


def main(model: str = "qwen3-coder-next:q4_K_M", temperature: float = 0.1, runs: int = 3, max_turns: int = 12) -> None:
    v2.MAX_AGENT_TURNS = max_turns

    for i in range(runs):
        mech = QueryMechanismV2(llm=TempOllamaClient(model=model, temperature=temperature))
        t0 = time.time()
        try:
            r = mech.ask(QUESTION)
            cited = {oid for oid in REAL_OBLIGATIONS if oid in r.answer}
            print(f"temp={temperature} run {i + 1}: {round(time.time() - t0, 1)}s, {len(r.tool_calls_made)} calls, cited {len(cited)}/7")
        except AgentTurnLimitExceeded:
            print(f"temp={temperature} run {i + 1}: {round(time.time() - t0, 1)}s, DID NOT CONVERGE")


if __name__ == "__main__":
    import sys

    temp = float(sys.argv[1]) if len(sys.argv) > 1 else 0.1
    main(temperature=temp)
