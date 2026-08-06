# Spike: Query & Check Mechanism (Chat Prototype)

Prototypes part 3 of the [top-level readme](../../readme.md)'s three-layer
architecture — the **Query & Check Layer** — on top of the graph proven out
in [`graph-ingestion3`](../graph-ingestion3/): natural-language questions
and situational checks against the Policy System knowledge graph, answered
with full provenance back to source regulation text.

## Status

Question catalog, test data, and golden answers/rubrics are all in place.
First query mechanism (deterministic templates) is implemented and passing.

1. [`example-questions.md`](./example-questions.md) — catalog of questions the target audiences would ask, tiered by traversal difficulty, each tagged with a grading method (exact-match / set-match / rubric).
2. [`synthetic-data-spec.md`](./synthetic-data-spec.md) — design for a fictional Pharma/Biotech company's (Helvex Biotech ApS) Policy/Standard/Control layer, sized to make the previously-blocked questions answerable and to include deliberate compliance gaps (not just a tidy fully-covered graph).
3. [`helvex_source.json`](./helvex_source.json) / [`build_helvex_graph.py`](./build_helvex_graph.py) — the data, generated and loaded into FalkorDB's `policy_system` graph alongside the real CRA/NIS2/GDPR data from `graph-ingestion3`. Golden answers for the previously-blocked questions have been spot-verified live (see the spec doc's "Status" section).
4. [`golden-answers.md`](./golden-answers.md) — computed golden values (Cypher + result) for every exact-match/set-match question, and rubric criteria grounded in real entities for every judgment question. Also where several stale catalog statuses (left over from before the Helvex load) and a few pre-extraction illustrative question names got corrected.
5. [`q-approach1.md`](./q-approach1.md) / [`query_mechanism_v1.py`](./query_mechanism_v1.py) / [`test_query_mechanism_v1.py`](./test_query_mechanism_v1.py) — a deterministic parameterized-Cypher-template router, no LLM in the loop. Passes 23/23: all 17 templatable questions match golden, all 6 rubric-graded questions correctly refuse (`NO_TEMPLATE_MATCH`) instead of guessing. Also where a real FalkorDB query-engine reliability issue was found and worked around (see that doc's "Result" section).

Not started yet: an LLM-in-the-loop or RAG-style mechanism for the 6 questions (M3, M5, H1, H3, H5, H6) that need semantic reasoning a template can't do, and the comparative evaluation across the full graded question set.

## Why start with questions, not architecture

"Build a chat interface over the graph" under-specifies the hard part. A
question like *"What roles does GDPR define?"* is one Cypher query. A
question like *"Is this new API endpoint compliant with GDPR Article 32?"*
requires mapping free-text about a system into a Capability, then reasoning
about whether a Control exists and passed — and today's graph has no
Control data wired to real regulations to reason over at all. Those two
questions can't share one naive "translate NL to Cypher and run it"
strategy. Cataloging question difficulty first is what tells us where a
single NL→Cypher translator is enough, and where we need retrieval +
multi-hop reasoning + an honest "I don't have that data yet."

## Data reality check (as of this spike's start)

The chat prototype can only be as honest as the graph it sits on. Current
state, loading `cra.json` + `nis2.json` + `gdpr.json` per
[`graph-ingestion3`](../graph-ingestion3/README.md):

| Layer | Status |
|---|---|
| Regulation → Role → Requirement → Obligation → Capability | Populated for real regulations: 3 Regulations, 13 Roles, 277 Requirements, 339 Obligations, 90 Capabilities |
| Cross-regulation Capability convergence | Workflow exists (`find_capability_duplicates.py` / `merge_capabilities.py`), but `capability_merges.json` is currently **empty** — no merges have actually been applied, so the 90 Capabilities above are not yet deduplicated across regulations |
| Capability → Policy → Standard → Control | Only exists in `policy_system_graph.json`, the **hand-built worked example** (2 Policies, 2 Standards, 2 Controls). Not connected to any real CRA/NIS2/GDPR data |

This means: today, questions that stop at Obligation/Capability are
answerable against real data. Anything that needs a Policy, Standard, or
Control — or a *merged* cross-regulation Capability — is either answerable
only against toy data or not answerable at all. See the status tags in
`example-questions.md`.

**Superseded by the Helvex spike below** — the Policy→Standard→Control row
above is no longer accurate. Helvex's synthetic layer governs 13 real
CRA/NIS2/GDPR capabilities (not just its own new one), so real
Requirement→...→Control chains now exist for GDPR and CRA. See
[`golden-answers.md`](./golden-answers.md) for the current, verified state
of every question in the catalog.

## Next steps

1. ~~Run `find_capability_duplicates.py` against the real, now-larger graph and curate `capability_merges.json` entries.~~ Done: ran it against the live 68-capability graph (0 candidates at the default 0.35 threshold; the 57 pairs down to 0.15 were manually reviewed and are all genuinely distinct, not duplicates). `capability_merges.json` correctly stays empty — real cross-regulation overlaps were already converged onto shared capability ids at extraction time (documented in `nis2-extraction-methodology.md` / `gdpr-extraction-methodology.md`), so there was nothing left for the merge workflow to catch. This resolved M8 (✅, confirmed live: Helvex and CRA both converge on `cap_security_logging_c4d9e2`) but *not* M3 — that's blocked by an extraction-scope gap (NIS2/GDPR have no distinct "Security Logging" capability at all), not an unmerged-duplicates one. See `example-questions.md`'s M3/M8 rows for detail.
2. ~~Compute/finalize golden answers and rubrics for every question in `example-questions.md` against the current live graph.~~ Done: see `golden-answers.md`. Along the way this corrected several catalog statuses that were stale from before the Helvex load (S7/S8, M7, H1/H2/H4/H5/H6/H7) and three questions (S2, S4, S6, and M2 by extension) that referenced illustrative entity names never present in the real extraction. H3 and H6 still need real query-mechanism reasoning (NL-to-Capability mapping, redundancy detection) rather than just a golden chain to point at — noted in `golden-answers.md`.
3. Decide the query architecture (NL→Cypher, RAG-over-graph, hybrid, or agentic tool-use) — likely per-tier rather than one mechanism for everything.
4. Prototype each candidate mechanism and score it against the graded question set — including whether it can tell a *stale* chain (deprecated Policy, planned Control) from a trustworthy one, per M7/H1's corrected findings.
5. If M3 needs to be a true three-way set-match: revisit the NIS2/GDPR extraction (prompts + methodology) for a logging-shaped obligation that may have been folded into a broader capability instead of extracted separately.
