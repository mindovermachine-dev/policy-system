<!-- © 2026 Cartman ApS. All rights reserved. -->
# Spike: Guided Fitness Pipeline

**Status:** Proposed 2026-08-11, not yet implemented.

## Purpose

Test a question to answer pipeline based on guiding the user to clarify their question in the context of the conceptual model. This solves the problem of users not knowing or understanding the model and anchors queries with the right terms.

Once the answer has been generated it is subjected to a verification process before the final answer is presented to the user.

Can we disprove the answer from the data in the graph? So rather than looking for confirmation, can we disprove it?


## Core loop

1. **Socratic narrowing.** User poses an open/global question. A skill uses Socratic dialogue to narrow it into a scoped, answerable question. The same skill then authors a candidate fitness function; the harness validates it against the static rubric (executable pair, independent re-query, scope bound, counting unit, falsification statement) before it is shown to the user. Authorship lives in the skill; rubric enforcement lives in the harness. Harness presents the refined question when it passes the rubric. User approves the question text before anything runs.

2. **Freehand retrieval.** Harness writes genuinely freehand, ad hoc Cypher
   against the graph to gather the data the question needs — deliberately
   NOT routed through `ps.py`'s `query template`/`query catalog` command
   preference, since routing would measure the CLI's templates rather than
   the LLM's retrieval creativity. The ps-domain schema rules (property
   names, edge directions, provenance discipline) apply as skill context;
   its command-routing rules do not. The CLI surface is reused only for
   its connection pattern and read-only guard (see Setup step 3).

3. **Answer construction.** Harness composes a candidate answer from the retrieved data. This step is by nature non-deterministic — currently an assumption, not a measurement (see note in Static rubric).

4. **Verification loop.** Fitness function's independent (query, predicate) executes against the candidate answer's claims. Fail → refine answer, retry. Pass or `max_turns` reached → stop.

5. **Falsification.** The LLM freely authors adversarial queries attempting
   to prove the refined answer wrong from the graph's data — deliberately
   not a fixed taxonomy, because a stated goal of this spike is to test
   LLM falsification creativity before deciding which angles (if any)
   deserve hardening into the rubric. Where the fitness function verifies
   the answer *within* the question's framing, falsification attacks the
   framing itself — this is what it adds beyond inverting the fitness
   predicate (the rubric already requires every fitness function to state
   its own falsifying data).
   Termination: capped at `max_falsification_attempts` (default 5) or the
   first landed disproof, whichever comes first. Every attempt that lands
   is reported; every attempt that misses is listed as "attempted, no
   discrepancy found." The pipeline never concludes "answer is correct" —
   it reports "answer survived N falsification attempts."

6. **Output.** Never a binary right/wrong verdict. Present the question, the refined answer, the retrieved data, and the fitness result (which predicate passed/failed, and why) — the user judges correctness, the pipeline supplies grounded evidence.

## Static rubric (governs step 1's authoring, not the questions themselves)

See [RUBRIC.md](./RUBRIC.md) for the versioned, standalone rubric that
step 1's fitness-function authoring is validated against, including its
measured-vs-assumed provenance note and growth discipline.

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
