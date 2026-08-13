<!-- © 2026 Cartman ApS. All rights reserved. -->
# Spike: Guided Fitness Pipeline pass 3

**Status:** Proposed 2026-08-11, not yet implemented.

## Purpose

Test a question to answer pipeline based on guiding the user to clarify their question in the context of the conceptual model. This solves the problem of users not knowing or understanding the model and anchors queries with the right terms.

Once the answer has been generated it is subjected to a verification process before the final answer is presented to the user.

Can we disprove the answer from the data in the graph? So rather than looking for confirmation, can we disprove it?


## Core loop
Is defined in the current tools/skills/policy-question.md file

This proposes adding a falsification step

1. **Falsification.** The LLM freely authors adversarial queries attempting
   to prove the refined answer wrong from the graph's data — deliberately
   not a fixed taxonomy, because a stated goal of this spike is to test
   LLM falsification creativity before deciding which angles (if any)
   deserve hardening into the rubric. Where the fitness function verifies
   the answer *within* the question's framing, falsification attacks the
   framing itself — this is what it adds beyond inverting the fitness
   predicate (the rubric already requires every fitness function to state
   its own falsifying data).
   
   Termination: capped at `max_falsification_attempts` or the first landed
   disproof, whichever comes first. The cap is scope-aware, per D6 (see
   PROGRESS.md): 1 attempt if the question's entities stay within the
   ingested compliance spine, 5 if `Policy`/`Standard`/`Control` (the
   customer-governed layer) are involved or the user asks for deeper
   scrutiny — a floor, not a skip, so falsification always runs at least
   once regardless of scope. Every attempt that lands is reported; every
   attempt that misses is listed as "attempted, no discrepancy found." The
   pipeline never concludes "answer is correct" — it reports "answer
   survived N falsification attempts."


## Setup

See [PROGRESS.md](./PROGRESS.md) for the decision log (D1-D3) behind these
steps.

1. Write `tools/skills/falsification-step.md` — a standalone reusable
   instruction, not a skill (matching `tools/skills/reasoning.md`'s
   precedent), defining the falsification process: freehand LLM-authored
   adversarial Cypher against `policy_system`, capped at
   `max_falsification_attempts` (default 5) or the first landed disproof,
   every attempt reported landed/missed, never concluding "correct" — only
   "survived N attempts." Reuses the same `ps.py cypher` read-only guarded
   connection surface `policy-question.md` already uses — no new CLI or
   harness.
2. Wire `tools/skills/policy-question.md` to invoke it: a new Process step
   after answer construction (step 6) that hands off the approved
   question, entities/edges, retrieved data, and constructed answer to
   `falsification-step.md`, and an Output template extended with a
   `Falsification:` section.
3. Update `policy-question.md`'s Purpose/Deliverable and Guardrails text so
   it no longer states falsification is "separate, not-yet-added" (that
   language was accurate before this spike, not after) — while still being
   explicit that no fitness-function/verification loop exists (steps 1b/4
   remain unbuilt).
4. Create `spikes/pipeline3/PROGRESS.md` to log decisions and pilot results
   as they happen.
5. Pilot run: execute the full pipeline (narrowing → retrieval → answer →
   falsification) against multiple real, previously-unseen questions — not
   just one — logging every attempt (landed/missed) in PROGRESS.md, and
   specifically watching for the "confirmation theater" failure mode named
   below (does falsification ever land, or only try weak angles?).
6. Review pilot results; capture (don't yet act on) any falsification angle
   that looks like a candidate for hardening into a fixed taxonomy/rubric
   later — that decision stays out of scope for this spike.

## Success Criteria

| Criterion | Threshold |
|---|---|
| No verdict inflation | Output never states pass/fail — only question, data, fitness result |
| Falsification termination | Falsification stops at the first landed disproof or `max_falsification_attempts`; every attempt is reported as landed or missed; never loops |
| Approval fidelity | User-approved query/predicate is exactly what executes — no silent drift between approval and execution |
| User can ask question in the VSCode chat | Every question approved is run through the full pipeline. |

## Failure Modes to Watch

- Freehand retrieval Cypher subtly over- or under-scoped (same failure class
  the compliance-decision-pipeline's narrowing/existence checks existed to
  catch — nothing here checks the checker's checker)
- Freehand falsification degrading into confirmation theater — the LLM
  "looking for" disproof but only trying weak angles. Watch for this in the
  run log: if falsification never lands across many runs, that is evidence
  about the falsifier, not about answer quality.
- Translation drift between the approved English question and the generated
  query
- Loop "passing" by drifting the answer to fit the fitness function's
  wording rather than fixing a genuine error
- Socratic step over-narrowing a question until it's trivial, defeating the
  point of asking it

## What This Is NOT

- Not a repeat of pipeline2.

## Deliverables

- Falsification step in policy question skill (`falsification-step.md` +
  `policy-question.md` wiring).
- PROGRESS.md decision log.
- Run log: falsification results against multiple real questions.
