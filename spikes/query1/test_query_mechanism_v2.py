#!/usr/bin/env python3
"""Test harness for query_mechanism_v2.py.

Per q-approach2.md's test plan: everything reachable without a live LLM
gets a real test against live data (tool implementations, the read-only
guard, whole_graph_stats' numbers against the same golden values
test_query_mechanism_v1.py verifies, the fallback-to-v1 path). The agent
loop's plumbing (tool results get fed back correctly, the loop terminates,
NoLLMConfigured fails loudly) is exercised with a scripted FakeLLMClient --
NOT a claim that a real LLM would answer the 13 targeted questions
correctly. That needs a real API key and is out of scope here, same
disclosure q-approach1.md already made for its own scoping decision.
"""

from query_mechanism_v2 import (
    AgentTurnLimitExceeded,
    LLMTurn,
    NoLLMConfigured,
    QueryMechanismV2,
    ReadOnlyViolation,
    ToolCall,
    _assert_read_only,
)


class FakeLLMClient:
    """Plays back a fixed sequence of turns, ignoring the actual message
    history/content. Verifies the agent LOOP works, not that any particular
    answer is correct -- see module docstring.
    """

    def __init__(self, turns: list[LLMTurn]):
        self._turns = list(turns)
        self.complete_calls = 0

    def complete(self, system, messages, tools) -> LLMTurn:
        self.complete_calls += 1
        if not self._turns:
            raise AssertionError("FakeLLMClient ran out of scripted turns")
        return self._turns.pop(0)


def check(label: str, ok: bool, note: str = "") -> bool:
    print(f"[{'PASS' if ok else 'FAIL'}] {label}" + (f" {note}" if note else ""))
    return ok


def main() -> int:
    passed, failed = 0, 0

    def record(ok: bool) -> None:
        nonlocal passed, failed
        passed += ok
        failed += not ok

    # -- read-only guard -----------------------------------------------
    for bad in ["MATCH (n) DELETE n", "MERGE (n:Capability {id:'x'})", "MATCH (n:Policy) SET n.status='approved'",
                "CREATE (n:Capability {id:'x'})", "MATCH (n) REMOVE n.status"]:
        try:
            _assert_read_only(bad)
            record(check(f"read-only guard blocks {bad!r}", False))
        except ReadOnlyViolation:
            record(check(f"read-only guard blocks {bad!r}", True))

    for good in ["MATCH (c:Capability) RETURN c.name", "MATCH (p:Policy)-[:SUPPORTED_BY]->(s) WHERE p.status='approved' RETURN s"]:
        try:
            _assert_read_only(good)
            record(check(f"read-only guard allows {good!r}", True))
        except ReadOnlyViolation:
            record(check(f"read-only guard allows {good!r}", False))

    # -- fallback to v1 (no LLM ever invoked) ---------------------------
    mech = QueryMechanismV2()  # default NoLLMConfigured -- must not be reached for a template hit
    r = mech.ask("What roles does GDPR define?")
    record(check("fallback-to-v1 path used for a template-matchable question",
                  r.mechanism == "v1-template" and r.template == "S1", note=f"(got mechanism={r.mechanism}, template={r.template})"))

    # -- NoLLMConfigured fails loudly on a genuinely out-of-scope question --
    try:
        mech.ask("Give me a one-paragraph summary of our overall compliance posture.")
        record(check("NoLLMConfigured raises on an out-of-scope question", False))
    except RuntimeError as e:
        record(check("NoLLMConfigured raises on an out-of-scope question", "ANTHROPIC_API_KEY" in str(e) or "LLM provider" in str(e)))

    # -- whole_graph_stats matches the same golden values test_query_mechanism_v1.py verifies --
    stats = mech.tools.whole_graph_stats()
    record(check("whole_graph_stats: capability governance split (13/55/68)",
                  stats["capabilities_total"] == 68 and stats["capabilities_ungoverned"] == 55
                  and stats["capabilities_governed"] == 13))
    record(check("whole_graph_stats: policy status breakdown (2 approved/1 draft/1 deprecated)",
                  stats["policy_status_breakdown"] == {"approved": 2, "draft": 1, "deprecated": 1}))
    record(check("whole_graph_stats: 1 overdue control",
                  len(stats["controls_overdue_for_review"]) == 1
                  and stats["controls_overdue_for_review"][0]["next_review_date"] == "2026-07-20"))
    record(check("whole_graph_stats: 1 planned control",
                  len(stats["controls_planned_not_yet_implemented"]) == 1))
    record(check("whole_graph_stats: 4 capabilities governed but zero implemented controls (M11)",
                  len(stats["capabilities_governed_but_zero_implemented_controls"]) == 4))
    record(check("whole_graph_stats: 57 GDPR chains, 31 current / 26 stale (M7)",
                  stats["gdpr_requirement_to_control_chains"] == {"total": 57, "current_evidence": 31, "stale_or_not_yet_current": 26}))

    # -- list_entities against live data --------------------------------
    policies = mech.tools.list_entities("Policy")
    record(check("list_entities('Policy') finds all 4 real policies",
                  set(policies) == {"Clinical Data Integrity Policy", "Data Protection & Security Policy",
                                     "Incident & Vulnerability Response Policy", "Legacy Asset & Personnel Security Policy"}))
    try:
        mech.tools.list_entities("Obligation")
        record(check("list_entities rejects an unsupported label", False))
    except ValueError:
        record(check("list_entities rejects an unsupported label", True))

    # -- run_cypher enforces read-only even through the tool entrypoint --
    try:
        mech.tools.run_cypher("MATCH (n:Capability) DETACH DELETE n")
        record(check("ToolBox.run_cypher enforces read-only", False))
    except ReadOnlyViolation:
        record(check("ToolBox.run_cypher enforces read-only", True))

    # -- agent loop plumbing, scripted (H9-shaped: no match -> honest refusal) --
    fake = FakeLLMClient([
        LLMTurn(tool_calls=[ToolCall(id="1", name="list_entities", arguments={"label": "Capability"})]),
        LLMTurn(text="No capability in the graph resembles rate-limiting or throttling; I can't determine a blocking verdict."),
    ])
    v2 = QueryMechanismV2(llm=fake)
    r = v2.ask("Does missing rate-limiting block a GDPR-relevant control?")
    record(check("agent loop: tool call -> tool result fed back -> final answer",
                  r.mechanism == "v2-agent" and "rate-limiting" in r.answer.lower() and len(r.tool_calls_made) == 1,
                  note=f"(answer={r.answer!r}, calls={len(r.tool_calls_made)})"))
    record(check("agent loop: LLM.complete invoked exactly twice (one tool round + one final)", fake.complete_calls == 2))

    # -- agent loop plumbing: a bad tool call surfaces as a tool error, doesn't crash the loop --
    fake2 = FakeLLMClient([
        LLMTurn(tool_calls=[ToolCall(id="1", name="run_cypher", arguments={"query": "MATCH (n) DELETE n"})]),
        LLMTurn(text="I can't run that query; it isn't read-only."),
    ])
    v2b = QueryMechanismV2(llm=fake2)
    r = v2b.ask("some open question with no template match")
    record(check("agent loop: a rejected tool call surfaces as an error, loop continues",
                  r.mechanism == "v2-agent" and "read-only" in r.answer.lower()))

    # -- agent loop: turn limit is enforced, not an infinite loop ------------
    endless = FakeLLMClient([
        LLMTurn(tool_calls=[ToolCall(id=str(i), name="whole_graph_stats", arguments={})]) for i in range(20)
    ])
    v2c = QueryMechanismV2(llm=endless)
    try:
        v2c.ask("another open question")
        record(check("agent loop: turn limit enforced", False))
    except AgentTurnLimitExceeded:
        record(check("agent loop: turn limit enforced", True))

    total = passed + failed
    print(f"\n{passed}/{total} passed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
