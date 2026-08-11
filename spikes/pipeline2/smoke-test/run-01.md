<!-- © 2026 Cartman ApS. All rights reserved. -->
# Smoke test — does falsification add anything over retrieval + construction alone?

Throwaway comparison, not a real pipeline2 deliverable. No rubric (RUBRIC.md),
no fitness-function authoring, no verification loop (step 4) — this isolates
steps 2+3 (freehand retrieval, answer construction) against the same steps
plus step 5 (freehand falsification), per user request, to sanity-check the
premise before investing further in the harness/skill/rubric machinery.

Executed via `spikes/e2e-pipeline/ps.py cypher` (read-only guarded escape
hatch, reused directly for this test — not forked, since this isn't the real
build) against FalkorDB `localhost:6379`, graph `policy_system`.

Question chosen fresh, deliberately avoiding CO-M2/CO-M4/PM-H3 (pipeline2's
reserved held-out set) and not a variant of any existing target case.

## Question

**"Which Policy governs the most Capabilities, and how many?"**

## Run A — retrieval + construction only (no falsification)

Freehand retrieval query:

```cypher
MATCH (c:Capability)-[:GOVERNED_BY]->(p:Policy)
RETURN p.title AS policy, p.status AS status, count(c) AS capability_count
ORDER BY capability_count DESC
```

Result:

| policy | status | capability_count |
|---|---|---|
| Data Protection & Security Policy | approved | 5 |
| Incident & Vulnerability Response Policy | approved | 4 |
| Legacy Asset & Personnel Security Policy | deprecated | 2 |
| Clinical Data Integrity Policy | draft | 2 |

**Constructed answer (Run A):** The Data Protection & Security Policy
governs the most capabilities, at 5 — ahead of the Incident & Vulnerability
Response Policy (4), with the Legacy Asset & Personnel Security Policy
(deprecated) and Clinical Data Integrity Policy (draft) tied at 2 each.

Nothing in Run A's process would surface any caveat beyond this — the
aggregation is a plain `ORDER BY ... DESC` over whatever `GOVERNED_BY` edges
exist, presented as if that's the complete picture.

## Run B — same question, + freehand falsification pass against Run A's answer

Three adversarial angles, chosen freehand without a fixed taxonomy (per D8):

**Attempt 1 — status framing.** Does restricting to non-deprecated,
non-draft (`status:'approved'`) policies change the ranking?

```cypher
MATCH (p:Policy {status:'approved'})<-[:GOVERNED_BY]-(c:Capability)
RETURN p.title AS policy, count(c) AS n ORDER BY n DESC
```
→ Data Protection & Security Policy still #1 at 5. **Missed — no
discrepancy.** The top claim is robust to this framing.

**Attempt 2 — double-counting.** Is any Capability governed by more than
one Policy, which would make "governs the most" ambiguous about
overlapping credit?

```cypher
MATCH (c:Capability)-[:GOVERNED_BY]->(p:Policy)
WITH c, count(p) AS pcount WHERE pcount > 1
RETURN count(c) AS multi_governed_count
```
→ `0`. **Missed — no discrepancy.**

**Attempt 3 — coverage/completeness.** How many Capabilities have *no*
governing Policy at all — does the graph actually support "governs the
most" as a claim about meaningful coverage, or just about a small subset?

```cypher
MATCH (c:Capability) WHERE NOT (c)-[:GOVERNED_BY]->(:Policy)
RETURN count(c) AS ungoverned_count
```
→ `55` (of 68 total Capabilities — cross-checked against the earlier
`MATCH (n) RETURN labels(n)` schema count). **Landed.** 81% of Capabilities
have zero `GOVERNED_BY` edge. Run A's literal claim (5 is the highest count
among policies that do have edges) is numerically correct, but presenting
it without this caveat invites a reader to assume governance coverage is
comprehensive when only 13/68 (19%) of Capabilities are governed by any
Policy in the graph at all.

**Falsification-qualified answer (Run B):** Same numeric answer as Run A,
plus: *"governs the most" is a claim about the 13 Capability-Policy edges
that exist — 55 of 68 Capabilities (81%) have no `GOVERNED_BY` edge at all,
so this doesn't indicate broad governance coverage.*

## Comparison

| | Run A (retrieval + construction) | Run B (+ falsification) |
|---|---|---|
| Numeric claim | 5 (Data Protection & Security Policy) | Same — never contradicted |
| Caveat surfaced | None | Governance-coverage gap (55/68 ungoverned), found on the 3rd attempt |
| Attempts landed | n/a | 1 of 3 |

## Takeaway

The falsification pass didn't overturn Run A's answer — 2 of 3 attempts
missed, and the numeric claim survived. But it did surface a real,
non-obvious framing risk (governance coverage, not fabrication) that Run A's
process has no mechanism to catch on its own, since Run A never asks "is
there data this claim is silently not about." That's the same *shape* of
gap CO-M2 was (omission/completeness, not fabrication) — but found here via
fresh freehand falsification against a brand-new question, not by reading
CO-M2 itself.

One caveat on this smoke test's own design: the same agent (me) played
retriever, answer-constructor, and falsifier in sequence, with the full
Run A result already in context when authoring Run B's attempts — closer to
how the real pipeline would actually run (same LLM, different steps) than a
blind-eval would be, but worth naming since it's not an independent second
party finding the gap.
