<!-- © 2026 Cartman ApS. All rights reserved. -->
# Static Rubric — Fitness Function Authoring

## Scoring scale

| Label | Value | Meaning |
|-------|-------|---------|
| Pass | 2 | Criterion fully satisfied by the fitness function's own stated text |
| Partial | 1 | Element is present but under-specified, vague, or incomplete |
| Fail | 0 | Element is absent, or contradicts the criterion |

## Criteria

| ID | Criterion | Pass (2) | Partial (1) | Fail (0) |
|----|-----------|----------|-------------|----------|
| FF-EXE-001 | Executable pair | Stated entirely as an executable (query, predicate) pair; no step in the pass criteria requires natural-language interpretation | A (query, predicate) pair is present but at least one natural-language pass criterion also appears | Pass criteria are natural-language description of expected behavior with no executable predicate |
| FF-REQ-001 | Independent re-query | Query is written independently of, and does not reuse or wrap, the query that produced the candidate answer | Query overlaps partially with the answer-producing query (e.g. shares a subquery or filter) but still executes separately | Query is the same query, or a trivial restatement, used to produce the answer being checked |
| FF-SCP-001 | Scope bound | States an explicit scope/narrowing bound that routes through the actual nodes/edges the claim depends on | Scope bound is stated but vaguely worded, or its routing through the relevant nodes/edges isn't explicit | No scope bound stated, or the claim is credited via a shared upstream node it doesn't actually route through |
| FF-CNT-001 | Counting unit | Explicitly states the counting unit / entity type the question is asking about, when applicable | Counting unit is implied by the query structure but not explicitly stated in the fitness function's own text | No counting unit stated or inferable, when the question requires one |
| FF-FAL-001 | Falsification statement | Explicitly states what data would falsify the claim, distinct from what would confirm it | A falsification angle is gestured at but not stated as concrete data/condition | No falsification statement, or the stated condition is a confirmation condition restated |

FF-CNT-001 is excluded from the gate (not scored Fail) when the question
has no countable unit.

## Pass gate

```
FF-EXE-001 Pass
AND FF-REQ-001 Pass
AND (FF-SCP-001 Pass or Partial)
AND (FF-CNT-001 Pass or Partial or N/A)
AND (FF-FAL-001 Pass or Partial)
```

FF-EXE-001 and FF-REQ-001 are binary by nature — a fitness function either
bottoms out in an executable pair or it doesn't, either re-queries
independently or it doesn't — so the gate requires Pass, not Partial;
anything less reopens the self-grading risk D2 exists to prevent.
FF-SCP-001, FF-CNT-001, and FF-FAL-001 are explicitness gradients (an
element can be present but under-specified), so the gate tolerates Partial
on those three.

A fitness function that fails the gate is not shown to the user; it goes
back to the authoring skill (D9).

## Scoring guardrails

Apply these on every rubric-validation pass without exception:

1. **Default to Fail.** If a criterion is not clearly supported by the
   fitness function's own stated text, score Fail. Do not infer intent or
   extrapolate from what the query would probably do.
2. **Conservative tie-breaker.** Pass vs. Partial → Partial. Partial vs.
   Fail → Fail.
3. **No downstream backfilling.** Score from the fitness function's own
   stated text only. A query happening to execute correctly does not
   retroactively satisfy a criterion the text doesn't state.
4. **Absent-element rule.** If a required element (e.g. a falsification
   statement) is entirely absent, score its criterion Fail (0), not
   Partial.
5. **Score before execution.** Rubric validation happens on the authored
   text, before the fitness function ever runs (step 1, before the
   refined question is shown to the user) — never re-score, or let a
   passing/failing execution result at step 4 change, a step 1 score.

