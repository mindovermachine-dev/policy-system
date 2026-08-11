<!-- © 2026 Cartman ApS. All rights reserved. -->
# Spike: Guided Fitness Pipeline

**Status:** Proposed 2026-08-11, not yet implemented.

## Purpose

Test a question to answer pipeline based on guiding the user to clarify their question in the context of the conceptual model. This solves the problem of users not knowing or understanding the model and achors queries witht the right terms.

Once the answer has been generated it is subjected to a verification process before the final answer is presented to the user.

 
 Can we disprove the answer from the data in the graph? So rather than looking for confirmation, can we disprove it?


## Core loop

1. **Socratic narrowing.** User poses an open/global question. A skill uses Socratic dialogue to narrow it into a scoped, answerable question, and jointly authors a refined question grounded in a static rubric. Harness also creates a deterministic fitness function to verify the answer. Harness presents the refined question when it passes the rubric. User approves the question text before anything runs.

2. **Freehand retrieval.** Harness writes ad hoc Cypher against the graph
   (via the existing `ps.py`-style CLI surface) to gather the data the
   question needs.

3. **Answer construction.** Harness composes a candidate answer from the retrieved data. This step is by nature non-deterministic.

4. **Verification loop.** Fitness function's independent (query, predicate) executes against the candidate answer's claims. Fail → refine answer, retry. Pass or `max_turns` reached → stop.

5. **Falsification** A final check of the refined answer is the Harness trying to prove the answer wrong by querying the graph. This is to avoid confirmation bias.

6. **Output.** Never a binary right/wrong verdict. Present the question, the refined answer, the retrieved data, and the fitness result (which predicate passed/failed, and why) — the user judges correctness, the pipeline supplies grounded evidence.

## Static rubric (governs step 1's authoring, not the questions themselves)

A fitness function is only valid if it:
- bottoms out in an executable (query, predicate) pair — no natural-language
  pass criteria (kills the self-grading risk of an LLM judging its own prose)
- re-queries independently of whatever query produced the answer
- states an explicit scope/narrowing bound (no claim credited via a shared
  upstream node it doesn't actually route through — SKILL.md rule 7)
- states the explicit counting unit / entity type the question is asking
  about, when applicable
- states what data would falsify the claim, not just what would confirm it

Rubric is static and versioned; fitness functions are not reused across questions — each is a matched pair with its question, since `compliance-decision-pipeline` already demonstrated no single check shape generalizes across question types (scope-match split into two mechanisms,
granularity split into two).

## Setup

1. Write the static rubric as a standalone, reviewable doc.

2. Build the Socratic question+fitness-authoring skill as a spike-local skill.

3. Build the freehand-retrieval CLI surface (reuse `ps.py`'s
   connection/query pattern, not its code).

4. Build the fitness-loop harness (execute fitness function, refine-or-stop).

5. Build falsification loop, attempting to disprove the refined answer


## Success Criteria

| Criterion | Threshold |
|---|---|
| Deterministic fitness functions | 100% of authored fitness functions bottom out in (query, predicate); zero LLM-judged prose criteria |
| Held-out generalization | Rubric-authored fitness functions for CO-M4 and PM-H3 actually catch those two known-unseen failures |
| No verdict inflation | Output never states pass/fail — only question, data, fitness result |
| Loop termination | Every run converges or reports best-effort within `max_turns`; never hangs |
| Approval fidelity | User-approved query/predicate is exactly what executes — no silent drift between approval and execution |
| User can ask question in the VSCode chat | Every question approved is run thru the full pipeline. |

## Failure Modes to Watch

- Freehand retrieval Cypher subtly over- or under-scoped (same failure class
  the old narrowing/existence checks existed to catch — nothing here checks
  the checker's checker)
- Translation drift between the approved English question and the generated
  query
- Rubric gaps on question shapes not yet seen (rubric needs a living-doc
  discipline, not a one-time write)
- Loop "passing" by drifting the answer to fit the fitness function's
  wording rather than fixing a genuine error
- Socratic step over-narrowing a question until it's trivial, defeating the
  point of asking it

## What This Is NOT

- Not a replacement or fix for `compliance-decision-pipeline` (stays frozen)
  or `e2e-pipeline` (separate bet, not abandoned)
- Not a judge ensemble — every fitness check stays deterministic, no
  LLM-as-judge
- Not human escalation / production wiring
- Not asserting correctness — this pipeline supplies evidence, the user
  renders judgment

## Deliverables

- Static rubric doc
- Socratic authoring skill (or spike-local stand-in)
- Freehand-retrieval + fitness-loop harness
- Run log against at least one genuinely new question.
