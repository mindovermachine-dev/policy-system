<!-- © 2026 Cartman ApS. All rights reserved. -->
# Spike: Skill Transfer

**Status:** Planned

---

## Purpose

Test the load-bearing assumption of the prototype architecture ([AD-6](../../docs/architecture/ps-prototype-architecture.md)): **a shipped agent skill grounds the harness LLM well enough that it reasons correctly about the Policy System domain model and the data it retrieves.**

If this fails, the entire "harness owns answers, subsystem owns facts" separation needs rethinking before any CLI or subsystem work proceeds.

## What We Already Know

From `query1/q-approach2.md`: moving the graph schema into the system prompt eliminated ID-pattern and relationship-direction Cypher failures entirely — tested directly, not assumed. But that was in `query_mechanism_v2.py`, a hand-rolled Python harness with a fixed system prompt.

**The unknown:** does the same grounding transfer to a real harness (VS Code + Copilot, or Pi + Ollama) where the "system prompt" is a skill file the agent loads, not a string we control?

## The Test

### Setup

1. **Write the PS Agent Skill** (`.agents/skills/ps-domain/SKILL.md` or equivalent) containing:
   - The 8 node labels and 8 relationship types from [ps-domain-concepts.md](../../docs/artifacts/ps-domain-concepts.md), with explicit edge directions
   - ID conventions (`obl_*`, `cap_*`, `{REG}_req_art_*`, etc.)
   - The two-layer content model (regulatory vs organizational)
   - Canonical query shapes: "if the user asks X, look up Y" routing patterns
   - The provenance citation discipline: every fact must carry its `source_ref` chain

2. **Load the Helvex graph** into FalkorDB (per `query1/build_helvex_graph.py`) so real data exists.

3. **Create the question sets using a blind harness.** To eliminate overfitting bias, a separate harness session (with NO access to the query spikes or their learnings) generates both question buckets:
   - **Input to blind harness**: only [ps-domain-concepts.md](../../docs/artifacts/ps-domain-concepts.md) (the domain model) + the Helvex graph schema + sample data
   - **Development set** (~20 questions): the blind harness generates questions across difficulty tiers and question shapes, then we compute golden answers against the live graph
   - **Held-out set** (~20 questions): same process, kept strictly separate — never seen during skill iteration, used once for final validation
   - **Critical**: the blind harness must NOT see `spikes/query1/`, `spikes/query2/`, or any spike learnings — it generates from first principles, like a real user would

4. **Run the development set** through a real harness with the skill loaded:
   - Fresh Copilot/Pi session per question (no context carryover)
   - Agent has the skill + a `run_cypher`-equivalent tool available
   - Grade against golden answers computed in step 3

5. **Iterate on the skill** based on development-set failures — but NEVER look at the held-out set during iteration.

6. **Final validation**: Run the held-out set once, with the final skill. This is the unbiased estimate of generalization.

### Success Criteria

| Criterion | Threshold |
|---|---|
| **Development set correctness** | 50% of development questions answered correctly or correctly refused (matching golden answers/rubrics) |
| **Held-out set correctness** | 50% of held-out questions answered correctly or correctly refused — same threshold as dev, since both are blind-generated |
| **No Cypher-shape errors** | Zero wrong-property, wrong-ID-pattern, or reversed-relationship failures across all runs |
| **Provenance citation** | Every answer cites the source chain (Regulation → article → Obligation → ...) |
| **Honest refusal** | Questions with no matching data result in "no such capability exists," not fabricated answers |

### Failure Modes to Watch

- The agent ignores the skill (doesn't load it, or loads but doesn't apply it)
- The agent freelances Cypher despite the skill's routing patterns
- The agent cites provenance incompletely (the H11 under-citing failure)
- The agent hallucinates numbers it already has in hand (the H13 failure)
- **Overfitting to the development set**: skill becomes a lookup table for known questions rather than generalizable grounding — the blind-generated held-out set is the check
- **Blind harness bias**: the question-generation harness itself has blind spots (e.g., only generates graph-shaped questions) — mitigated by reviewing its output for diversity before finalizing the sets

These are the exact failure modes from the query spikes — if they recur with the skill, the skill isn't working.

## What This Is NOT

- Not building the CLI (that's `cli-tool-semantics`)
- Not building the subsystem service (that's `end-to-end-slice`)
- Not testing union-of-N, validators, or other LLM-quality mitigations — those are product questions, not architecture questions
- Not reusing the query1 question catalog — a blind harness generates fresh questions to avoid overfitting to known failure modes

## Deliverables

- The PS Agent Skill file (reusable — this is a prototype artifact, not throwaway)
- `dev-questions.md` and `held-out-questions.md` (~20 each) generated by the blind harness, with golden answers computed against the live graph
- Results tables: development set and held-out set, each with pass/fail/partial + notes
- A verdict: AD-6 holds, or AD-6 needs revision
