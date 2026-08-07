# Mining pass: catalog roots and DSL primitive coverage from the 39 golden queries

Per `q-approach4.md` §7 fixes 1 and 8 — before building Candidate D's catalog
or Candidate B's DSL, mine the operation shapes actually present in all 39
Cypher queries in [`../query1/golden-answers.md`](../query1/golden-answers.md),
rather than assuming coverage top-down. This is that pass.

## What every golden query actually touches

Walking all 39 golden Cypher blocks (not just H1/H3/H5), every one of them is
built from the same nine relationship types and eight node labels, and every
multi-hop query is a **contiguous sub-path** of one of two chains:

```
Chain A (governance):
  (Regulation)-[DEFINES]->(Role)-[HAS]->(Obligation)-[REQUIRES]->(Capability)
    -[GOVERNED_BY]->(Policy)-[SUPPORTED_BY]->(Standard)-[IMPLEMENTED_BY]->(Control)

Chain B (requirement text), joins Chain A at Obligation:
  (Regulation)-[EXPRESSES]->(Requirement)-[SATISFIED_BY]->(Obligation)

Plus one standalone edge: (Regulation)-[SUPERSEDED_BY]->(Regulation)
```

No golden query needs a relationship type, a node label, or a hop shape
outside these three structures. That's a stronger, measured version of
§10.1's abstract point (schema-correctness ≠ answer-correctness) — here it
says something narrower and more useful: **the traversal shapes themselves
are not diverse**. 39 questions, 3 structures.

## Per-question classification

| Q | Golden shape | Sub-path of | Needs free-text resolution? | Needs judgment beyond the joined rows? |
|---|---|---|---|---|
| S1 | Reg→Role | A (prefix) | no | no |
| S2 | Requirement lookup | B (leaf) | no | no |
| S3 | Reg→Role→Obligation | A (prefix) | no (role exact) | no |
| S4 | Obligation→Capability | A (mid) | no (obligation exact) | no |
| S5 | Regulation props | — (leaf) | no | no |
| S6 | Requirement→Obligation | B | no | no |
| S7 | Capability→Policy | A (mid) | no | no |
| S8 | Policy→Standard | A (mid) | no | no |
| S9 | Policy→Standard→Control | A (suffix) | no | no |
| S10 | Control lookup, fuzzy title | A (leaf) | yes (title substring) | no |
| M1 | Obligation→Capability, aggregate | A (mid) | no | no (pure count) |
| M2 | Requirement→Obligation→Capability | A+B joined | no (id exact) | no |
| M3 | Capability←Obligation, cross-reg, **absence claim** | A (mid, reversed) | yes ("Security Logging"-type) | **yes** — must state NIS2/GDPR have none, not omit |
| M4 | Reg→Role→Obligation, aggregate by role | A (prefix) | no | no |
| M5 | Role sets per regulation, **semantic compare** | A (prefix) ×2 | no | **yes** — no structural join proves similarity |
| M6 | Obligation confidence filter | A (leaf) | no | no |
| M7 | Full chain A, req↔obl via B | A+B, full | no | no (trust-flag is deterministic) |
| M8 | Capability shared across regs | A (mid, reversed) | no | no (pure set) |
| M9/M12 | Control filter | A (leaf) | no | no |
| M10 | Policy status aggregate | A (mid) | no | no |
| M11 | Capability→...→Control, OPTIONAL | A (suffix), OPTIONAL | no | no (statuses list is deterministic) |
| M13 | Policy→Standard filter | A (mid) | no | no |
| M14 | Draft Policy→Capability, **GDPR-relevance filter** | A (mid) | no | **yes** — no edge encodes "GDPR-relevant" |
| H1 | Full chain A, filtered to Art. 32, **compliance verdict** | A+B, full | no (article id exact) | verdict is deterministic *given* the trust flags — see below |
| H2 | Capability NOT→Policy | A (mid, negated) | no | no |
| H3 | Free-text scenario → 2 Capabilities → **compliance verdict per capability** | A (mid) | yes (scenario→capability, ×2) | **yes** — which capability the scenario "fails" isn't a joinable fact |
| H4 | Control lookup, fuzzy title | A (leaf) | yes (title substring) | no |
| H5 | Reg SUPERSEDED_BY + Policy staleness | standalone + A (mid) | no | no (staleness is the status field itself) |
| H6 | Capability lookup + reverse-obligation aggregate, **redundancy check** | A (mid, reversed) | yes ("SBOM") | **yes** — "already redundantly covered" is a judgment on the (empty) result |
| H7 | Control date filter | A (leaf) | no | no |
| H8 | Free-text scenario → **multiple** Capabilities, no verdict | A (mid) ×N | yes (scenario→capabilities, open set) | mild — deciding *which* capabilities are relevant isn't a single lookup |
| H9 | Free-text → Capability, **expected no match** | A (mid) | yes, must resolve to none | no (once resolver honestly returns nothing) |
| H10 | no Service/System node | **outside all 3 structures** | — | schema gap, not reachable |
| H11 | Free-text → Capability → reverse walk to Obligation→Role→Reg | A (mid→prefix, reversed) | yes ("MFA") | no (once resolved, backward walk is deterministic) |
| H12–H14 | whole-graph aggregate, **narrative synthesis** | cross-cutting, no single anchor | no | **yes** — genuinely open narration |
| H15 | no status-transition history | **outside all 3 structures** | — | schema gap, not reachable |

## What this settles

**Candidate D's catalog root is exactly Chain A ∪ Chain B, one denormalized
table.** Not several catalogs keyed by different roots (Capability, Role,
Regulation, Policy, as `q-approach4.md` §3 speculated) — one table with every
node id/status/property from both chains as columns, since every golden
query's multi-hop shape is a contiguous slice of the *same* two chains. A
question anchors on whichever column it needs (Capability, Policy, Regulation,
Control, ...) and filters/aggregates from there. This is stronger than §3's
original framing and simpler to build.

**Free-text resolution is needed for 9 of 39** (S10, H4 — Control title
fuzzy match, already handled by substring `CONTAINS` in v1; M3, M5, M6-n/a,
M14, H6, H3 ×2, H8 ×N, H9, H11 — Capability/obligation-type free text). Of
these, the harder "resolve novel free text to a Capability" case is exactly
H3/H8/H9/H11/H6 — 5 questions, not the full 9. The bake-off (next task)
scopes to these 5.

**Judgment beyond the joined rows is needed for 8 of 39**: M3 (absence
claim), M5 (semantic role comparison — no shared schema field), M14 (GDPR
relevance — no edge encodes it), H3 (which capability a scenario "fails" —
not a joinable fact, though *which capabilities are in scope* is), H6
(redundancy judgment on an empty result), H8 (open-ended relevance set),
H12–H14 (open narration). **H1 is not in this list** — despite being a
rubric question, its "partial compliance" verdict is a deterministic function
of the trust-annotated rows already computed by `_annotate_trust`-style logic
(count clean/partial/stale/ungoverned sub-clauses) — confirmed by reading
`golden-answers.md`'s own H1 entry, which states the verdict as a row-by-row
enumeration, not a fresh inference. Same for H11's "hypothetical against
today's real evidence" caveat — that's just citing the resolved capability's
current Policy/Control status columns, still catalog data.

**Revised routing, sharper than `q-approach4.md` §5's original table**:

| Question | Catalog D reaches it deterministically (no LLM at all)? |
|---|---|
| H1 | **Yes** — chain + trust flags + a fixed verdict-classification function |
| H2 | Yes (already v1, unaffected) |
| H11 | **Yes** — resolve capability, walk backward, cite current governance status |
| S10, H4 | Yes (already v1, unaffected) |
| H5 | **Yes** — SUPERSEDED_BY edge + Policy/Standard status columns, no free text to resolve |
| H8 | **Partial** — catalog supplies the candidate Capability rows once resolved, but "which capabilities are relevant to a PII-storing microservice" is an open resolution problem (multiple, fuzzy), not a single deterministic lookup; needs either a broadened resolver (return top-k, not top-1) or v2 fallback to pick from candidates |
| H9 | **Yes** — the golden answer *is* "no match," which an honest resolver returning empty produces directly, no narration needed |
| H3 | **Partial** — resolving the two capabilities the scenario touches is doable via the same resolver as H8/H11, but stating *which one the scenario fails* ("logs access" passes, "doesn't encrypt" fails) requires matching scenario sub-clauses to specific capabilities, not just one free-text string — this is the one place a small amount of free-text *reasoning*, not just resolution, is unavoidable |
| M3, M5, M14, H6 | **No** — genuine judgment calls per the table above; route to v2 (`q-approach4.md`'s own conclusion, unchanged) |
| H12–H14 | **No** — unchanged, `whole_graph_stats` + narration |

This mostly matches `q-approach4.md` §5's original assignment, with two
corrections now that it's measured rather than assumed: **H1 and H11 don't
need *any* LLM step**, not even for narration — the design's §5 table
under-claimed what D reaches by leaving room for a "citation-completeness
gate applied to its narration," implying narration was still LLM-driven.
It doesn't need to be. **H3 and H8 are only partially reachable** — H3
needs a small scenario→capability judgment step even after resolution
(not a full agentic loop, but not zero reasoning either); H8's open
candidate set is a resolver-shape problem (top-k, not top-1) rather than a
reasoning one.

**No DSL primitive set gets mined here because none is needed.** §7 fix 1's
originally-planned DSL mining (Candidate B: `walk`, `filter`, `aggregate`,
`resolve_entity`) doesn't apply — every golden query is a slice of one
pre-joined table, so there's no live traversal plan left to compile once the
catalog exists. This is itself the measured finding `q-approach4.md` §5 and
§7 fix 9 asked for: Candidate B's go/no-go gate is **no-go**, decided here
rather than deferred to a later report, because the catalog's single-table
shape leaves no golden-query shape it doesn't already cover structurally.
The only two open gaps (H3's sub-clause matching, H8's open candidate set)
are resolver/reasoning gaps, not traversal-grammar gaps — a DSL compiler
wouldn't address either one.
