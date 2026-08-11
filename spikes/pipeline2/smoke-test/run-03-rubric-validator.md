<!-- © 2026 Cartman ApS. All rights reserved. -->
# Smoke test 3 — does the rubric-validation harness (D11) actually discriminate?

Throwaway test, not a real pipeline2 deliverable. Tests step 1's rubric
enforcement (D9/D11) in isolation from Socratic narrowing and from
authoring quality: 6 fitness functions were hand-authored (by the
orchestrator, grounded in real questions/data from run-02), 5 with exactly
one deliberately planted flaw (one per RUBRIC.md v2 criterion) and 1 fully
clean. Each was sent, unlabeled, to its own isolated validator subagent
with no knowledge of the others or of which (if any) flaw was planted —
only RUBRIC.md v2 itself as instructions.

## Results

| FF | Planted flaw | Expected gate | Actual gate | Match? |
|----|--------------|---------------|--------------|--------|
| A | none (clean) | PASS | PASS (5/5 Pass) | ✅ |
| B | FF-EXE-001 — predicate is prose ("looks reasonable"), not executable | FAIL | FAIL (FF-EXE-001 scored **Partial**, not Fail — see note) | ✅ gate, ⚠️ criterion score |
| C | FF-REQ-001 — query is explicitly the same query that produced the answer | FAIL | FAIL (FF-REQ-001 correctly caught; validator *also* independently flagged FF-FAL-001 as Fail — a bare "value other than 20" negation, unplanted but defensible) | ✅, plus a legitimate bonus catch |
| D | FF-SCP-001 — scope bound entirely absent | FAIL | FAIL (exact match) | ✅ |
| E | FF-CNT-001 — counting unit entirely absent | FAIL (expected, per Guardrail 4) | **PASS** — FF-CNT-001 scored Partial ("implied by query structure"), and since the gate tolerates Partial on this criterion, the flawed fitness function cleared the gate | ❌ |
| F | FF-FAL-001 — falsification statement is the confirming condition restated | FAIL | FAIL (exact match) | ✅ |

## The one real finding: RUBRIC.md v2 contradicts itself on absent elements

FF-E's "Counting unit" field was written verbatim as `(not stated)` —
genuinely absent, not just unmentioned. Guardrail 4 ("Absent-element
rule") says this must score Fail (0), not Partial. But the Criteria
table's own Partial-tier definition for FF-CNT-001 says: *"Counting unit
is implied by the query structure but not explicitly stated in the
fitness function's own text"* — which is exactly FF-E's situation, since
the query's `count(DISTINCT c)` does imply the unit. The validator
followed the specific per-criterion definition over the general
guardrail — a defensible reading of what's actually written, not
carelessness. Because FF-CNT-001 is one of the three criteria whose gate
branch tolerates Partial, this let a fitness function that admits its own
counting unit is unstated still reach the user.

This is the kind of gap a cheap test is supposed to catch: not "can the
LLM read a rubric," but "does the rubric's own wording actually enforce
what we intended." Guardrail 4 and the Criteria table need to be
reconciled — either the Partial-tier wording for FF-CNT-001 (and any
other criterion with an "implied but unstated" Partial tier) needs an
explicit carve-out for the case where the field is *explicitly marked
absent* by the author vs. *simply never mentioned*, or Guardrail 4 needs
to state it overrides the per-criterion table, not just coexist with it.

FF-B's Partial-not-Fail score (a 100%-prose predicate with zero
executable structure, which by the Criteria table's own Fail definition
—"no executable predicate"— should have scored Fail) is a milder version
of the same ambiguity. It didn't change FF-B's gate outcome (FF-EXE-001
requires strict Pass regardless of Partial-vs-Fail), but it's the same
root cause: the Partial-tier wording is written broadly enough to catch
cases that arguably belong in Fail.

## Takeaway

5 of 6 gate verdicts were exactly right, including catching a genuinely
subtle case (FF-C's bare falsification negation) the test didn't
deliberately plant. The harness is not rubber-stamping — it engages with
the rubric's actual wording and reasons about it. But the one miss isn't
noise: it's a specific, fixable defect in RUBRIC.md v2's own text
(Criteria table vs. Guardrail 4 disagreement on absent elements),
caught before any real fitness function was ever authored by an actual
skill. Same shape of value as run-01/run-02: cheap, isolated tests
surfacing real gaps before investing in the harness/skill build-out.
