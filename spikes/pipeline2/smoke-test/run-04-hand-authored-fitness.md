<!-- © 2026 Cartman ApS. All rights reserved. -->
# Smoke test 4 — can a fitness function be hand-authored, and does it hold up when actually executed?

Throwaway test, not a real pipeline2 deliverable. Unlike run-03 (which tested
the rubric-*validation* harness on synthetic, planted-flaw fitness
functions), this test authored real fitness functions from scratch against
two already-approved `Question`/`Entities`/`Edges` fixtures
([fixtures.md](./fixtures.md): NQ-001, NQ-002 — produced by
`policy-question` steps 1-4, not invented for this test), then executed
both the answer-producing query and the fitness function live against
`policy_system`, before any fitness-authoring skill exists. Purpose: get
concrete evidence, before investing in that skill, that hand-authoring
against RUBRIC.md v2 alone can produce something that (a) passes the gate
honestly and (b) actually verifies something true when run against real
data — not just rubric-legal text.

## NQ-001 — "How many distinct Role nodes are defined (via DEFINES) by the
active version of the CRA regulation, and what are their names?"

Candidate answer (live, freehand retrieval): `N=6` — Manufacturer,
Open-source software steward, Authorised representative, Importer,
Distributor, Substantial modifier.

**First draft failed its own gate.** FF-REQ-001 (independent re-query)
honestly scored Partial, not Pass — the query was separately authored
(inbound traversal, cardinality pre-check, row-per-role instead of
pre-aggregated collect/count) but necessarily walks the same `DEFINES`
edge as the answer query, since that's the only edge the schema offers for
this claim. Gate: **FAIL** (FF-REQ-001 requires strict Pass).

**Live execution caught a second, independent defect the rubric score
didn't:** the query's stated predicate 1 ("exactly one active CRA
regulation") was *not actually verifiable from the query's own output* — a
join structure meant any active-CRA regulation with zero Roles would
silently vanish before the predicate ever saw it. FF-EXE-001 had scored
Pass ("fully executable") but executable isn't the same as "returns what
the predicate needs." Caught only by running it, not by text review.

**Fix:** added a query returning `size(active_cra_regs)` directly instead
of inferring cardinality from join survival. Re-run confirmed
`active_reg_count = 1` (`CRA-1.0`), directly from the fitness query's own
output. Full predicate set then confirmed true against the live candidate
answer (role-name sets match exactly; distinct-node count = 6 = claimed
`N`).

## NQ-002 — "For the active version of CRA, which Requirements do not have
a complete implementation chain...?"

The multi-hop fixture: Requirement→Obligation→Capability→Policy→Standard→
Control, gapped if any link is missing or reaches a Policy not `approved`,
a Standard not `implemented`/`reviewed`, or a Control not
`implemented`/`reviewed`.

Candidate answer (live): **all 74** active-CRA Requirements gapped.
Verified this wasn't a query bug by tracing one Requirement
(`CRA-1.0_req_art_13.2`) by hand: its Capability node has no `GOVERNED_BY`
edge to any Policy at all. Separately confirmed the graph's only 10
complete Requirement→Control chains anywhere all belong to the internal
`ENGPRAC-3.0` regulation, none to CRA — real seed-data immaturity, not a
retrieval defect.

**Fitness function passed the gate on the first attempt, including a
genuine FF-REQ-001 Pass** (not just Partial) — the independent query used
`UNWIND` over the valid status-value combinations plus a real `MATCH`
(existence-by-enumeration), structurally distinct from the answer query's
`OPTIONAL MATCH` + `collect` + `CASE WHEN` + `reduce` aggregation. No
shared subquery fragment between the two. Executed live: the "covered"
set (Requirements with ≥1 complete good-status chain) returned **0 rows**,
matching `all_req_ids − covered_set = 74` exactly against the claimed gap
list.

## Finding: independent-re-query quality is gated by claim structure, not authoring effort

NQ-001 (single edge type, one traversal path) could only reach Partial on
FF-REQ-001 no matter how differently the query was shaped — the schema
offers exactly one path to the claim, so "independent" can only vary
*how* that one path is walked, not walk a different path. NQ-002 (six
edge types, a real branching/enumeration space) reached genuine Pass,
because the completeness question has enough structure to support a truly
different derivation strategy (enumerate valid end-states vs. aggregate
over all discovered paths). This predicts a general rule for the
not-yet-built fitness-authoring skill: **expect FF-REQ-001 to cap at
Partial for single-hop factual lookups**, and treat that as a structural
ceiling, not an authoring failure to keep pushing against.

## Incidental finding: FalkorDB Cypher dialect gaps

Two syntax forms failed that are legal in mainstream Cypher (Neo4j-style):
- `WHERE NOT EXISTS { MATCH ... }` (subquery block) — `Invalid input '('`.
- A `WHERE NOT (pattern)` predicate using an inline property map that
  references an externally-bound variable (e.g.
  `{status: $var}` inside the pattern) — `Unable to resolve filtered
  alias`. Plain `MATCH ... WHERE prop = $var` works fine; the failure is
  specific to filtered aliases inside pattern-predicates.

Neither was previously documented anywhere in the repo. Same genre as
run-02's incidentally-found `count(*)` aggregation bug (Finding 3,
[LEARNINGS.md](./LEARNINGS.md)) — infrastructure risk for whoever
freehand-authors Cypher against this graph, independent of pipeline2's own
design questions.

## Takeaway

Hand-authoring against RUBRIC.md v2 alone — no fitness-authoring skill,
no harness — produced two fitness functions that were live-executed
against real retrieved data and correctly verified a real candidate
answer, including catching a genuine defect (NQ-001's unverifiable
predicate 1) that rubric self-scoring alone had missed. Same shape of
value as run-01/02/03: a cheap test surfacing something real (a query
defect, a structural rubric-difficulty pattern, an infra gap) before
investing in the skill/harness build-out.
