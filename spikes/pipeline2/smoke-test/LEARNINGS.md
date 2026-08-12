<!-- © 2026 Cartman ApS. All rights reserved. -->
# Smoke-test learnings so far

Synthesis across [run-01.md](./run-01.md), [run-02-subagents.md](./run-02-subagents.md),
[run-03-rubric-validator.md](./run-03-rubric-validator.md), and
[run-04-hand-authored-fitness.md](./run-04-hand-authored-fitness.md).
These were cheap, throwaway tests of individual pipeline layers, run
before investing in the real skill/harness/rubric build-out (Setup steps
2-5) — not pipeline2 deliverables themselves. Motivation: check whether
the design's core bets (falsification adds value; the rubric-validation
harness actually discriminates; a fitness function can be hand-authored
and actually work) hold up before building around them.

## What was tested, and what wasn't

Tested: step 5 (falsification) against a steps-2+3 baseline (run-01,
run-02); step 1's rubric-validation harness in isolation (run-03, D9/D11);
hand-authoring a real fitness function from an approved fixture and
executing it live against real retrieved data (run-04).

Not tested: Socratic narrowing (the open-question-to-scoped-question
dialogue, step 1's other half) and the full verification *loop* (step 4)
— specifically, its refine-on-fail path. Socratic narrowing needs a
simulated naive-user dialogue, not a single-shot question. Run-04 executed
a real fitness function against a real candidate answer and got a true
verdict, but both fitness functions passed on the first try — the loop's
refine-and-retry behavior when a fitness function actually fails against
the answer remains unexercised.

## Finding 1 — Falsification's value is difficulty-gated, not uniform

Across run-01 (1 question) and run-02 (5 questions, increasing
difficulty, isolated retriever/falsifier subagents): **0 of 12
falsification attempts landed on easy/medium questions** (run-02 Q1-3).
Landings only appeared on the harder end: run-01's single question
(completeness/coverage gap) and run-02's Q5 (the hardest, 1 of 3
attempts). Q4 (medium-hard) landed nothing against the actual claim but
surfaced an unrelated infrastructure bug (see Finding 3).

**Implication:** falsification isn't free value on every question — on
straightforward exhaustive aggregations it mostly confirms. It earns its
keep on questions requiring multi-hop reasoning or cross-referencing.

## Finding 2 — When falsification lands, it's not wrong numbers, it's wrong assumptions

Every landed finding across both runs was a **numerically correct answer
with a misleading implicit assumption**, never a fabrication or
miscount:
- run-01: "governs the most Capabilities (5)" was correct, but 55 of 68
  Capabilities (81%) have no governing Policy at all — a completeness/
  coverage gap.
- run-02 Q5: "shared across 3 distinct Regulations" was correct by node
  count, but 2 of those 3 Regulation nodes are versions of the same
  underlying SOP (linked by `SUPERSEDED_BY`) — a node-identity-vs-real-
  world-identity gap.

Two data points, same shape as `compliance-decision-pipeline`'s CO-M2
(omission, not fabrication) but two distinct sub-patterns (coverage,
identity-collapsing). Not enough to harden into RUBRIC.md yet (per its
own growth-discipline clause) — noted here as the run-log evidence that
clause is waiting for.

## Finding 3 — Isolation between roles matters, and surfaces free findings

run-02's retriever/falsifier subagent split (falsifier blind to the
retrieval query) produced a materially stronger result than run-01's
single-agent version — a real independent disproof, not the same agent
second-guessing itself. Real design implies step 5 needs genuine
separation from steps 2-3 in the actual harness, not just "later in the
same context."

Also surfaced, incidentally: a reproducible FalkorDB `count(*)`
aggregation bug that under-reports when co-aggregated with multiple
`DISTINCT` columns (run-02 Q4). Didn't corrupt that answer, but is
infrastructure risk for whoever builds the real freehand-retrieval CLI
surface — independent of pipeline2's own design questions.

## Finding 4 — The rubric-validation harness (D11) mostly discriminates correctly, and its one miss is a real bug in RUBRIC.md itself

run-03: 6 hand-authored fitness functions (1 clean, 5 each with one
planted flaw, one per RUBRIC.md v2 criterion), each scored blind by its
own isolated validator subagent. **5 of 6 gate verdicts matched exactly**,
including a legitimate bonus catch (a bare falsification negation with no
concrete detail, unplanted but defensible under the rubric's own
wording). The harness engages with the rubric's actual text rather than
rubber-stamping.

**The 1 miss is a specification bug, not a validator error.** RUBRIC.md
v2's Guardrail 4 ("absent element → Fail") contradicts its own Criteria
table (FF-CNT-001's Partial tier explicitly allows "implied by query
structure but not explicitly stated"). An admittedly-absent counting unit
cleared the gate because FF-CNT-001 tolerates Partial and the validator
reasonably followed the more specific per-criterion wording. **Not yet
fixed** — flagged to the user, decision pending.

## Finding 5 — Hand-authoring works, and independent-re-query quality is gated by claim structure, not authoring effort

run-04: two fitness functions hand-authored against RUBRIC.md v2 (no
fitness-authoring skill exists yet), each executed live against
`policy_system` — for NQ-001 (single-edge claim: Roles defined by active
CRA) and NQ-002 (six-edge chain claim: Requirement→...→Control
completeness).

NQ-001's first draft **failed its own gate** (FF-REQ-001 honestly scored
Partial — the schema offers exactly one path to the claim, so no
independently-authored query can walk a genuinely different one) and its
gate-legal query still had a latent defect only live execution caught:
predicate 1 ("exactly one active CRA regulation") wasn't actually
derivable from the query's own output, because a join could silently drop
a zero-match regulation before the predicate ever saw it. Same shape as
Finding 4: rubric-legal text is not the same as a query that returns what
its own predicate needs.

NQ-002 passed cleanly on the first attempt, including a genuine FF-REQ-001
**Pass** (not Partial) — six edge types gave real room for a structurally
different derivation (enumerate valid end-states via `UNWIND`+`MATCH`
vs. the answer query's `OPTIONAL MATCH`+aggregate-`reduce`), with no
shared subquery fragment.

**Implication for the not-yet-built fitness-authoring skill:** expect
FF-REQ-001 to cap at Partial for single-hop factual lookups as a
structural ceiling, not an authoring failure — don't tune the skill to
chase a Pass that the claim's own shape makes unreachable.

Incidentally surfaced, same genre as Finding 3's `count(*)` bug: FalkorDB
rejects `EXISTS { }` Cypher subqueries and pattern-predicates with inline
property maps referencing externally-bound variables
(`Unable to resolve filtered alias`) — undocumented anywhere before this
run, real constraint on freehand retrieval's toolset.

## Meta-learning

Every one of these three cheap tests found something real and specific
before any money was spent on the actual skill/harness build (Setup
steps 2-5): a completeness gap, a node-identity gap, an infrastructure
bug, and a rubric self-contradiction. That's a working argument for
continuing to test each remaining layer this way (Socratic narrowing,
end-to-end step 4) before building it for real, rather than building
first and discovering gaps later.

## Open items

- RUBRIC.md v2's Guardrail 4 vs. Criteria table conflict — unresolved.
- Socratic narrowing — untested, needs a different test shape (simulated
  naive-user dialogue).
- Full verification *loop*'s refine-on-fail path — still untested; run-04
  executed a real fitness function against a real candidate answer and
  got a true verdict, but both passed on the first try.
- FalkorDB Cypher dialect gaps found in run-04 (`EXISTS {}` subqueries,
  filtered-alias pattern-predicates) — not yet folded into any guidance
  for freehand retrieval authors.
- Sample sizes remain small: 6 questions for step 5, 6 fitness functions
  for step 1's rubric validation, 2 fitness functions for run-04's
  hand-authoring test. Findings are real but not yet a basis for broad
  claims.
