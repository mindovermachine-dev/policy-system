<!-- © 2026 Cartman ApS. All rights reserved. -->
# Spike: Skill Transfer

**Status:** Planned (question catalog ready in `docs/test-data/`; skill not yet written)

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

   **Content provenance rule**: routing patterns derive from the *mechanism shapes* (template router, Candidate D catalog, introspection, freehand fallback) and the domain model — never from the question catalog in `docs/test-data/` or query1's `example-questions.md`. The skill must generalize by construction; if a pattern exists only because a known question needs it, it doesn't belong in the skill. The held-out set is the check that this rule held.

2. **Verify the graph is loaded — no load step.** FalkorDB's `policy_system` graph already holds the real CRA/NIS2/GDPR data (per `graph-ingestion3`) plus the Helvex synthetic Policy/Standard/Control layer (per `query1/build_helvex_graph.py`). Confirm FalkorDB is running and the graph answers a smoke query; nothing else.

3. **Question sets already exist — do not regenerate.** The catalog is built audience-first, tiered by difficulty, with a deliberate natural/canonical register mix, split into two halves:
   - **Development set**: [dev-questions.md](../../docs/test-data/dev-questions.md) + [dev-answers.md](../../docs/test-data/dev-answers.md) — golden values and grading criteria, anchored to the dataset reference date **2026-08-01** (evaluation runs must supply that anchor, or date-relative answers drift)
   - **Held-out (blind) set**: lives in a **separate repo** — deliberately absent from this workspace so nothing in skill iteration can pollute it (and vice versa). Frozen; fetched only for the single final-validation run in step 6.
   - **Critical**: both halves were generated blind — from [ps-domain-concepts.md](../../docs/artifacts/ps-domain-concepts.md), the graph schema, and sample data only, with no access to `spikes/query1/` or `spikes/query2/` learnings. Any *regeneration* must enforce that isolation structurally: run the generator in a clean workspace containing only the allowed inputs — not in this repo, where the spike folders (golden answers, question catalogs, failure-mode docs) are reachable and would leak into generation.
   - Do not add, reword, or re-tier questions during skill iteration. If a question class needs fixing, fix it in the dev half and treat the blind half as unseen.

4. **Run the development set** through a real harness with the skill loaded:
   - Fresh Copilot/Pi session per question (no context carryover)
   - Agent has the skill + a `run_cypher`-equivalent tool available
   - Grade against [dev-answers.md](../../docs/test-data/dev-answers.md)

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
- Not building or extending the question catalog — it already exists in `docs/test-data/` and is frozen; this spike consumes it, it does not revise it

## Deliverables

- The PS Agent Skill file (reusable — this is a prototype artifact, not throwaway)
- Results tables: development set and held-out set, each with pass/fail/partial + notes (the question sets themselves are already delivered — see step 3)
- A verdict: AD-6 holds, or AD-6 needs revision
