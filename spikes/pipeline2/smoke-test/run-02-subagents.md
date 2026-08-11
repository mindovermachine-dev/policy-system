<!-- © 2026 Cartman ApS. All rights reserved. -->
# Smoke test 2 — subagent-isolated retriever vs. independent falsifier, 5 questions of increasing difficulty

Throwaway comparison, not a real pipeline2 deliverable (no RUBRIC.md, no
verification loop). Fixes run-01's isolation caveat: a **retriever+
constructor subagent** does freehand retrieval and builds the answer; a
separate **falsifier subagent** sees only the question and that answer —
never the retrieval query or reasoning — and independently writes its own
freehand Cypher (up to 4 attempts, no fixed taxonomy, stop early on a
landed disproof). All 10 subagents ran via `ps.py cypher` (read-only
guarded) against FalkorDB `localhost:6379`, graph `policy_system`.

Questions chosen fresh, avoiding CO-M2/CO-M4/PM-H3 and existing
compliance-decision-pipeline target cases (e.g. SA-H2).

## Results

| # | Difficulty | Question | Run A answer (headline) | Falsification result |
|---|---|---|---|---|
| 1 | Easy | Which Role has the most Obligations? | Controller, 148 (vs. Processor 55, Manufacturer 48) | **0/4 landed.** Recount, duplicate-edge, direction/aliasing, and staleness checks all missed — 342 Obligations partition cleanly 1:1 across Roles. |
| 2 | Easy-medium | Any Requirements not satisfied by any Obligation? | No — 0 of 281 unsatisfied | **0/4 landed.** Direction, label-mismatch, "satisfied but not meaningfully" (status/confidence), and duplicate-edge checks all missed. |
| 3 | Medium | Which Regulation has the most unsatisfied Requirements? | None — empty set (0 across all 5 Regulations) | **0/4 landed.** Direction, degenerate-zero-Requirement-Regulation, property-naming landmine (`reg.name` doesn't exist), and placeholder-Obligation checks all missed. |
| 4 | Medium-hard | Which Regulation requires the most distinct Capabilities (3-hop)? | GDPR-1.0, 42 (vs. CRA-1.0 29, NIS2-1.0 19) | **0/4 landed as disproof of the answer** — coverage, node-duplication, and independent re-derivation all missed; headline numbers hold. **But attempt 4 surfaced a real defect**: a reproducible FalkorDB `count(*)` aggregation bug that under-reports when co-aggregated with multiple `DISTINCT` columns. Didn't corrupt this answer, but is a methodology caveat for any future count query against this graph. |
| 5 | Hard | Which Capabilities are required by Obligations tracing to >1 Regulation (3-hop + cross-source)? | 20 Capabilities; 5 shared across 3 sources, 15 across 2 | **1/3 landed** (stopped early after landing). Top-line count (20, split 5/15) survived re-derivation and membership checks. **Landed**: "Security Logging" was counted as shared across 3 distinct Regulations, but two of those three Regulation nodes (`HELVEX-SOP-1.0`, `HELVEX-SOP-2.0`) are versions of the *same* underlying SOP, linked by `SUPERSEDED_BY` — the real source-count is 2, not 3. A specific claim in the answer was genuinely wrong. |

## Takeaway

Falsification yield tracked difficulty as hypothesized: **0 landings on
questions 1–3 (single/simple two-hop, exhaustive aggregations or clean
empty-set claims), 0 landings on the actual claim in question 4 (but a
real infrastructure defect surfaced as a side effect), and 1 genuine
landed disproof on question 5** (the only one requiring cross-source
identity reasoning — recognizing that two Regulation nodes are really the
same regulatory source at different versions). The harder question wasn't
just "more likely to be wrong" in a vague sense — the landed defect was a
specific reasoning gap (node identity vs. real-world source identity) that
retrieval + construction alone had no mechanism to catch, structurally the
same kind of gap run-01's coverage finding was.

Two things worth carrying into the real design:
- **Node-identity-vs-real-world-identity** (versioned/superseded nodes
  double-counted as distinct sources) is a concrete falsification angle
  worth naming, alongside run-01's completeness/coverage angle — not yet
  a rubric criterion (per RUBRIC.md's growth discipline, needs more than
  one data point before hardening in), but now has a second one.
- Q4's FalkorDB `count(*)` aggregation defect is infrastructure risk
  orthogonal to this spike's design questions — worth a note wherever the
  freehand-retrieval CLI surface gets built, independent of what happens
  with pipeline2 itself.

Isolation note: unlike run-01, retriever and falsifier were genuinely
separate subagents with no shared context — the falsifier only ever saw
the question and the answer text, never the query. Q5's landing is
therefore a stronger result than run-01's (real independent-party
disproof, not the same agent second-guessing itself).
