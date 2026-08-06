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

import re

import query_mechanism_v2 as v2_module
from query_mechanism_v2 import (
    GRAPH_SCHEMA,
    SCHEMA_RELATIONSHIP_DIRECTIONS,
    AgentTurnLimitExceeded,
    LLMTurn,
    NoLLMConfigured,
    QueryMechanismV2,
    ReadOnlyViolation,
    ToolCall,
    _assert_read_only,
    correct_relationship_directions,
    extract_entity_ids,
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

    # -- deterministic relationship-direction correction (q-approach3.md) ----
    # Real H1 failure shape (q-approach2.md's "Result" table): `pol` bound to
    # Policy in an earlier MATCH, SUPPORTED_BY reversed in a later one.
    h1_query = (
        "MATCH (pol:Policy {id:'pol_data_protection_security_policy_8e4c18'}) "
        "MATCH (pol)<-[:SUPPORTED_BY]-(:Standard) RETURN pol.id"
    )
    corrected, corrections = correct_relationship_directions(h1_query)
    record(check("direction corrector fixes the real H1 reversed SUPPORTED_BY",
                  "(pol)-[:SUPPORTED_BY]->(:Standard)" in corrected and len(corrections) == 1,
                  note=f"(corrected={corrected!r})"))

    # Real H11 failure shape: reversed DEFINES, both nodes labeled inline.
    h11_query = "MATCH (reg:Regulation {id:'CRA-1.0'})<-[:DEFINES]-(role:Role) RETURN role.name"
    corrected, corrections = correct_relationship_directions(h11_query)
    record(check("direction corrector fixes the real H11 reversed DEFINES",
                  "(reg:Regulation {id:'CRA-1.0'})-[:DEFINES]->(role:Role)" in corrected and len(corrections) == 1,
                  note=f"(corrected={corrected!r})"))

    # Already-correct multi-hop chain (the exact shape whole_graph_stats.py's
    # GDPR-chain query uses) -- must be left byte-for-byte untouched.
    correct_chain = (
        "MATCH (req:Requirement)-[:SATISFIED_BY]->(o:Obligation)-[:REQUIRES]->(c:Capability)"
        "-[:GOVERNED_BY]->(p:Policy)-[:SUPPORTED_BY]->(s:Standard)-[:IMPLEMENTED_BY]->(ctrl:Control) RETURN ctrl.id"
    )
    corrected, corrections = correct_relationship_directions(correct_chain)
    record(check("direction corrector leaves an already-correct 5-hop chain untouched",
                  corrected == correct_chain and corrections == []))

    # Correct backward traversal (Control back to Standard, arrow already
    # right) -- legitimate reverse-order querying, not a mistake, must not be
    # touched.
    backward_correct = "MATCH (ctrl:Control {id:'x'})<-[:IMPLEMENTED_BY]-(s:Standard) RETURN s.id"
    corrected, corrections = correct_relationship_directions(backward_correct)
    record(check("direction corrector leaves a correct backward traversal untouched",
                  corrected == backward_correct and corrections == []))

    # Unresolvable labels -- no basis to correct, left alone even though reversed.
    reversed_no_labels = "MATCH (a)<-[:SUPPORTED_BY]-(b) RETURN a, b"
    corrected, corrections = correct_relationship_directions(reversed_no_labels)
    record(check("direction corrector leaves an unresolvable (unlabeled) hop untouched",
                  corrected == reversed_no_labels and corrections == []))

    # Undirected relationship -- already matches both directions, nothing to fix.
    undirected = "MATCH (p:Policy)-[:SUPPORTED_BY]-(s:Standard) RETURN p, s"
    corrected, corrections = correct_relationship_directions(undirected)
    record(check("direction corrector leaves an undirected relationship untouched",
                  corrected == undirected and corrections == []))

    # Multi-type and variable-length patterns -- explicitly out of scope (see
    # module docstring), left untouched even when reversed.
    multi_type = "MATCH (p:Policy)<-[:SUPPORTED_BY|OTHER]-(s:Standard) RETURN p, s"
    corrected, corrections = correct_relationship_directions(multi_type)
    record(check("direction corrector leaves a multi-type relationship untouched",
                  corrected == multi_type and corrections == []))

    var_length = "MATCH (p:Policy)<-[:SUPPORTED_BY*1..2]-(s:Standard) RETURN p, s"
    corrected, corrections = correct_relationship_directions(var_length)
    record(check("direction corrector leaves a variable-length relationship untouched",
                  corrected == var_length and corrections == []))

    # Same-label relationship (SUPERSEDED_BY: Regulation->Regulation) -- labels
    # alone can't tell "from" from "to", so this is left alone even reversed.
    superseded = "MATCH (a:Regulation)<-[:SUPERSEDED_BY]-(b:Regulation) RETURN a, b"
    corrected, corrections = correct_relationship_directions(superseded)
    record(check("direction corrector leaves a same-label (SUPERSEDED_BY) relationship untouched",
                  corrected == superseded and corrections == []))

    # A stray function-call token shaped like a node pattern (count(s)) must
    # not trigger a false-positive correction elsewhere in the query.
    with_function_call = (
        "MATCH (p:Policy)-[:SUPPORTED_BY]->(s:Standard) "
        "WITH p, count(s) AS n WHERE n > 0 RETURN p.id, n"
    )
    corrected, corrections = correct_relationship_directions(with_function_call)
    record(check("direction corrector unaffected by a count(...) call elsewhere in the query",
                  corrected == with_function_call and corrections == []))

    # SCHEMA_RELATIONSHIP_DIRECTIONS (structured, what the corrector checks
    # against) must stay in sync with GRAPH_SCHEMA's prose (what the model
    # reads) -- every canonical pair below must appear as a forward arrow
    # somewhere in the prose, whitespace/line-wrapping aside.
    schema_prose_compact = re.sub(r"\s+", "", GRAPH_SCHEMA)
    schema_prose_ok = all(
        f"(:{frm})-[:{rel}]->(:{to})" in schema_prose_compact
        for rel, (frm, to) in SCHEMA_RELATIONSHIP_DIRECTIONS.items()
    )
    record(check("SCHEMA_RELATIONSHIP_DIRECTIONS matches every relationship named in GRAPH_SCHEMA's prose",
                  schema_prose_ok))

    # -- direction correction wired into ToolBox.run_cypher, against live data --
    corrected_live = mech.tools.run_cypher(
        "MATCH (pol:Policy {id:'pol_data_protection_security_policy_8e4c18'}) "
        "MATCH (pol)<-[:SUPPORTED_BY]-(s:Standard) RETURN s.id ORDER BY s.id"
    )
    direct_live = mech.tools.run_cypher(
        "MATCH (pol:Policy {id:'pol_data_protection_security_policy_8e4c18'})-[:SUPPORTED_BY]->(s:Standard) "
        "RETURN s.id ORDER BY s.id"
    )
    record(check("ToolBox.run_cypher: a reversed live SUPPORTED_BY query is corrected and matches the direct query's rows",
                  corrected_live["rows"] == direct_live["rows"] and len(corrected_live["rows"]) == 3
                  and "direction_corrected" in corrected_live,
                  note=f"(rows={corrected_live['rows']}, note={corrected_live.get('direction_corrected')})"))
    record(check("ToolBox.run_cypher: an already-correct live query has no direction_corrected key",
                  "direction_corrected" not in direct_live))

    # -- agent loop plumbing, scripted (H9-shaped: no match -> honest refusal) --
    # union_runs=1 throughout this block: these tests isolate single-round-trip
    # plumbing (tool call -> tool result -> final answer, error surfacing, turn
    # limit), which is orthogonal to union-of-N's combination logic (tested
    # separately below) -- pinning it keeps scripted turn counts exact.
    fake = FakeLLMClient([
        LLMTurn(tool_calls=[ToolCall(id="1", name="list_entities", arguments={"label": "Capability"})]),
        LLMTurn(text="No capability in the graph resembles rate-limiting or throttling; I can't determine a blocking verdict."),
    ])
    v2 = QueryMechanismV2(llm=fake, union_runs=1)
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
    v2b = QueryMechanismV2(llm=fake2, union_runs=1)
    r = v2b.ask("some open question with no template match")
    record(check("agent loop: a rejected tool call surfaces as an error, loop continues",
                  r.mechanism == "v2-agent" and "read-only" in r.answer.lower()))

    # -- agent loop: turn limit is enforced, not an infinite loop ------------
    endless = FakeLLMClient([
        LLMTurn(tool_calls=[ToolCall(id=str(i), name="whole_graph_stats", arguments={})]) for i in range(20)
    ])
    v2c = QueryMechanismV2(llm=endless, union_runs=1)
    try:
        v2c.ask("another open question")
        record(check("agent loop: turn limit enforced", False))
    except AgentTurnLimitExceeded:
        record(check("agent loop: turn limit enforced", True))

    # -- extract_entity_ids: prefixed and Regulation/Requirement id shapes ----
    ids = extract_entity_ids(
        "See obl_deploy_multi_factor_authentication_and_secured_communication_138a1f "
        "under GDPR-1.0_req_art_32.1a, unrelated to CRA-1.0."
    )
    record(check("extract_entity_ids finds a prefixed id and a requirement id, not the bare regulation id twice",
                  ids == {"obl_deploy_multi_factor_authentication_and_secured_communication_138a1f",
                          "GDPR-1.0_req_art_32.1a", "CRA-1.0"},
                  note=f"(got {ids})"))
    record(check("extract_entity_ids on plain prose with no ids returns empty",
                  extract_entity_ids("Several obligations require this.") == set()))

    # -- union-of-N: best-coverage run kept verbatim, gaps appended mechanically --
    # Real ids not required here -- extract_entity_ids only cares about shape,
    # per the module docstring above.
    ALPHA, BETA, GAMMA = "obl_test_alpha_aaaaaa", "obl_test_beta_bbbbbb", "obl_test_gamma_cccccc"
    union_fake = FakeLLMClient([
        LLMTurn(text=f"Affected: {ALPHA}, {BETA}."),  # run 1: 2/3 -- best individual coverage
        LLMTurn(text=f"Affected: {GAMMA}."),  # run 2: 1/3, but the only run with GAMMA
        LLMTurn(text=f"Affected: {ALPHA}."),  # run 3: 1/3
    ])
    v2d = QueryMechanismV2(llm=union_fake, union_runs=3)
    r = v2d.ask("some open union-of-N question")
    record(check("union-of-N: samples the model union_runs times", union_fake.complete_calls == 3))
    record(check("union-of-N: reports all 3 runs converged", r.runs_sampled == 3))
    record(check("union-of-N: keeps the best-coverage run's answer verbatim as the primary text",
                  r.answer.startswith(f"Affected: {ALPHA}, {BETA}."), note=f"(answer={r.answer!r})"))
    record(check("union-of-N: mechanically appends the id only a lower-coverage run found",
                  r.union_ids_added == [GAMMA] and GAMMA in r.answer, note=f"(union_ids_added={r.union_ids_added})"))
    record(check("union-of-N: doesn't re-append what the primary run already cited",
                  ALPHA not in r.union_ids_added and BETA not in r.union_ids_added))

    # -- union-of-N: a non-converging run is dropped, not a hard failure -----
    # Lower MAX_AGENT_TURNS so the non-converging run's endless tool-call loop
    # only costs 2 scripted turns instead of the real default's 8.
    original_max_turns = v2_module.MAX_AGENT_TURNS
    v2_module.MAX_AGENT_TURNS = 2
    try:
        partial_fake = FakeLLMClient([
            LLMTurn(tool_calls=[ToolCall(id="1", name="whole_graph_stats", arguments={})]),  # run 1: never finalizes
            LLMTurn(tool_calls=[ToolCall(id="2", name="whole_graph_stats", arguments={})]),
            LLMTurn(text=f"Affected: {ALPHA}."),  # run 2: converges
            LLMTurn(text=f"Affected: {ALPHA}, {BETA}."),  # run 3: converges, best coverage
        ])
        v2e = QueryMechanismV2(llm=partial_fake, union_runs=3)
        r = v2e.ask("some open union-of-N question with one non-convergent sample")
        record(check("union-of-N: a non-converging sample is excluded, not a hard failure",
                      r.runs_sampled == 2, note=f"(runs_sampled={r.runs_sampled})"))
        record(check("union-of-N: still picks the best-coverage surviving run",
                      r.answer.startswith(f"Affected: {ALPHA}, {BETA}.")))

        # -- union-of-N: if EVERY sample fails to converge, that propagates --
        all_fail_fake = FakeLLMClient([
            LLMTurn(tool_calls=[ToolCall(id=str(i), name="whole_graph_stats", arguments={})]) for i in range(6)
        ])
        v2f = QueryMechanismV2(llm=all_fail_fake, union_runs=3)
        try:
            v2f.ask("some open question where every sample times out")
            record(check("union-of-N: raises when every sampled run fails to converge", False))
        except AgentTurnLimitExceeded:
            record(check("union-of-N: raises when every sampled run fails to converge", True))
    finally:
        v2_module.MAX_AGENT_TURNS = original_max_turns

    total = passed + failed
    print(f"\n{passed}/{total} passed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
